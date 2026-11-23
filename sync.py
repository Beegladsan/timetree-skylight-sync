#!/usr/bin/env python3
"""
TimeTree -> Skylight one-way calendar sync.

Fetches every event from a single TimeTree calendar (handling TimeTree's
chunked ~300-events-per-request pagination), converts them into a valid
RFC 5545 .ics feed with:

  * a STABLE, deterministic per-event UID (the TimeTree event uuid), so
    regenerating the feed never creates duplicate/"new" events downstream;
  * correct all-day handling (VALUE=DATE with an exclusive DTEND);
  * recurrence passthrough (RRULE / EXDATE / RDATE) when TimeTree exposes it;
  * bounded VTIMEZONE components for every referenced TZID, so the feed is
    RFC-valid *and* DST-correct for recurring events.

It then applies fail-loud guardrails (empty feed, structural validation,
duplicate-UID detection, and a "count dropped too far" check against the
currently-published feed) and only writes output when everything passes,
so a broken/partial run never overwrites a known-good feed.

Built on the maintained `timetree-exporter` package, which owns the
reverse-engineered TimeTree web API (auth + pagination + event model).
This module contributes the orchestration, hardening, and publishing shape.

Secrets (email/password or session token) are read from the environment
and are NEVER written to stdout, logs, or the output files.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from importlib.metadata import PackageNotFoundError, version as pkg_version
from pathlib import Path

import requests
from requests.exceptions import HTTPError, RequestException
from icalendar import Calendar as ICalendar
from icalendar.prop import vDate, vDatetime, vDuration

from timetree_exporter.api.auth import (
    AuthenticationError,
    InvalidCredentialsError,
    RateLimitAuthenticationError,
    login,
)
from timetree_exporter.api.calendar import TimeTreeCalendar
from timetree_exporter.calendar import Calendar
from timetree_exporter.exporter import build_single_calendar
from timetree_exporter.utils import add_bounded_timezones_before_events

TOOL_NAME = "timetree-skylight-sync"
# Stable PRODID (intentionally NOT version-stamped, so the feed identity does
# not churn between tool releases).
PRODID = "-//timetree-skylight-sync//TimeTree to Skylight//EN"
REFRESH_INTERVAL = timedelta(hours=12)
# Below this baseline we don't trust the drop-guard (too small to be meaningful).
MIN_BASELINE_FOR_DROP_GUARD = 5

log = logging.getLogger("timetree_skylight_sync")


class SyncError(RuntimeError):
    """A loud, fail-the-run condition. Raising this exits non-zero without writing output."""


# --------------------------------------------------------------------------- #
# Config helpers
# --------------------------------------------------------------------------- #
def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _tool_version() -> str:
    try:
        return pkg_version("timetree_exporter")
    except PackageNotFoundError:  # pragma: no cover - defensive
        return "unknown"


def _lib_version(name: str) -> str:
    try:
        return pkg_version(name)
    except PackageNotFoundError:  # pragma: no cover - defensive
        return "unknown"


def _run_url() -> str | None:
    server = os.environ.get("GITHUB_SERVER_URL")
    repo = os.environ.get("GITHUB_REPOSITORY")
    run_id = os.environ.get("GITHUB_RUN_ID")
    if server and repo and run_id:
        return f"{server}/{repo}/actions/runs/{run_id}"
    return None


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #
def authenticate() -> str:
    """Return a TimeTree `_session_id`, reusing a token if provided, else logging in.

    Prefers TIMETREE_SESSION_ID (avoids hammering the login endpoint on every
    scheduled run, which can trip rate-limits/lockouts). Falls back to
    email+password. Never logs the credential values.
    """
    session_id = os.environ.get("TIMETREE_SESSION_ID", "").strip()
    if session_id:
        log.info("Authenticating with a provided session token (TIMETREE_SESSION_ID).")
        return session_id

    email = os.environ.get("TIMETREE_EMAIL", "").strip()
    password = os.environ.get("TIMETREE_PASSWORD", "")
    if not email or not password:
        raise SyncError(
            "No credentials found. Set TIMETREE_SESSION_ID, or both "
            "TIMETREE_EMAIL and TIMETREE_PASSWORD."
        )

    log.info("Authenticating to TimeTree with email/password for %s", _mask_email(email))
    try:
        session_id = login(email, password)
    except InvalidCredentialsError as exc:
        raise SyncError("TimeTree rejected the email/password. Check the secrets.") from exc
    except RateLimitAuthenticationError as exc:
        raise SyncError(
            "TimeTree rate-limited the login. Wait and retry, or switch to "
            "TIMETREE_SESSION_ID to avoid repeated logins."
        ) from exc
    except AuthenticationError as exc:
        raise SyncError(f"TimeTree login failed: {exc}") from exc
    except RequestException as exc:
        raise SyncError(f"Network error contacting TimeTree during login: {exc}") from exc

    if not session_id:
        raise SyncError(
            "TimeTree login returned no session id. The auth API shape may have changed."
        )
    return session_id


def _mask_email(email: str) -> str:
    """Return a non-reversible hint of the email for logs (never the full value)."""
    local, _, domain = email.partition("@")
    head = local[:1] if local else ""
    return f"{head}***@{domain}" if domain else "***"


# --------------------------------------------------------------------------- #
# Calendar selection
# --------------------------------------------------------------------------- #
def load_calendars(api: TimeTreeCalendar) -> list[dict]:
    """Return active (non-deactivated) calendars, failing loudly on API/auth errors."""
    try:
        metadatas = api.get_metadata()
    except HTTPError as exc:
        raise SyncError(
            "Could not fetch the calendar list. The session token/credentials may be "
            "invalid or expired, or the TimeTree API changed."
        ) from exc
    except RequestException as exc:
        raise SyncError(f"Network error fetching the calendar list: {exc}") from exc
    except (KeyError, ValueError, TypeError) as exc:
        raise SyncError(
            f"Unexpected calendar-list response (TimeTree API may have changed): {exc}"
        ) from exc

    return [m for m in metadatas if m.get("deactivated_at") is None]


def _format_calendar_listing(metadatas: list[dict]) -> str:
    lines = []
    for m in metadatas:
        lines.append(
            f"  id={m.get('id')}  code={m.get('alias_code')}  "
            f"name={m.get('name') or '(unnamed)'}"
        )
    return "\n".join(lines) if lines else "  (none)"


def select_calendar(api: TimeTreeCalendar, selector: str) -> Calendar:
    """Resolve a single calendar by numeric id, alias code, or exact name."""
    active = load_calendars(api)
    if not active:
        raise SyncError("No active TimeTree calendars found on this account.")

    def matches(m: dict) -> bool:
        return selector in {str(m.get("id")), str(m.get("alias_code")), str(m.get("name"))}

    chosen = [m for m in active if matches(m)]
    if not chosen:
        raise SyncError(
            f"No calendar matched {selector!r}.\nAvailable calendars:\n"
            f"{_format_calendar_listing(active)}"
        )
    if len(chosen) > 1:
        raise SyncError(
            f"{selector!r} matched {len(chosen)} calendars; use the numeric id instead.\n"
            f"{_format_calendar_listing(chosen)}"
        )
    meta = chosen[0]
    log.info("Selected calendar: %r (id=%s, code=%s)", meta.get("name"), meta.get("id"),
             meta.get("alias_code"))
    return Calendar(api, meta)


def select_calendars(
    api: TimeTreeCalendar,
    selectors: list[str] | None = None,
    all_calendars: bool = False,
) -> list[Calendar]:
    """Resolve one or more calendars to export and merge into a single feed.

    With ``all_calendars`` true, returns every active calendar on the account.
    Otherwise each entry in ``selectors`` (an id, alias code, or exact name) is
    resolved and the results are merged, de-duplicated by calendar id.
    """
    active = load_calendars(api)
    if not active:
        raise SyncError("No active TimeTree calendars found on this account.")

    if all_calendars:
        chosen = list(active)
    else:
        chosen = []
        seen: set = set()
        for raw in selectors or []:
            selector = str(raw).strip()
            if not selector:
                continue
            matches = [
                m for m in active
                if selector in {str(m.get("id")), str(m.get("alias_code")), str(m.get("name"))}
            ]
            if not matches:
                raise SyncError(
                    f"No calendar matched {selector!r}.\nAvailable calendars:\n"
                    f"{_format_calendar_listing(active)}"
                )
            for m in matches:
                if m.get("id") not in seen:
                    seen.add(m.get("id"))
                    chosen.append(m)
        if not chosen:
            raise SyncError("No calendar selected.")

    for m in chosen:
        log.info("Including calendar: %r (id=%s, code=%s)", m.get("name"), m.get("id"),
                 m.get("alias_code"))
    return [Calendar(api, m) for m in chosen]


# --------------------------------------------------------------------------- #
# Feed construction
# --------------------------------------------------------------------------- #
def build_feed(calendars: list[Calendar], feed_name: str) -> tuple[bytes, int, int]:
    """Fetch + convert one or more calendars into a single serialized .ics feed.

    Returns (ics_bytes, fetched_event_count, published_vevent_count).
    Events from every calendar are merged and de-duplicated by TimeTree uuid.
    Pagination (chunk/since) is handled inside the library; a partial-page
    failure surfaces here as an exception rather than a silent truncation, and
    if ANY calendar fails to fetch the whole run fails (so a broken partial feed
    never overwrites a good one).
    """
    all_events: list = []
    merged_labels: dict = {}
    seen_uuids: set = set()

    for calendar in calendars:
        try:
            events = calendar.get_events()
        except HTTPError as exc:
            raise SyncError(
                f"Failed to fetch events for calendar {calendar.name!r} "
                "(auth/session invalid, or TimeTree API changed)."
            ) from exc
        except RequestException as exc:
            raise SyncError(
                f"Network error fetching events for {calendar.name!r}: {exc}"
            ) from exc
        except (KeyError, ValueError, TypeError) as exc:
            raise SyncError(
                f"Events response for {calendar.name!r} was missing expected fields "
                f"(TimeTree API may have changed): {exc}"
            ) from exc

        added = 0
        for event in events:
            uid = event.get("uuid")
            if uid and uid in seen_uuids:
                continue  # same event shared across calendars — keep one copy
            if uid:
                seen_uuids.add(uid)
            all_events.append(event)
            added += 1
        log.info("Fetched %d events from %r (%d new after de-dup).",
                 len(events), calendar.name, added)

        try:
            labels = calendar.get_labels()
            if labels:
                merged_labels.update(labels)
        except Exception as exc:  # labels are cosmetic (CATEGORIES); never fail over them
            log.warning("Could not fetch labels for %r (continuing): %s", calendar.name, exc)

    fetched = len(all_events)
    log.info("Merged %d event(s) across %d calendar(s).", fetched, len(calendars))

    cal = build_single_calendar(all_events, merged_labels)
    _apply_calendar_headers(cal, feed_name)
    # RFC 5545 requires VALUE=DATE on all-day DTSTART/DTEND. The upstream library
    # emits a bare date (no param), which strict parsers reject as an invalid
    # DATE-TIME; stamp the parameter back on so the feed is strictly valid.
    _ensure_date_value_params(cal)
    # Inject bounded VTIMEZONE components for every TZID used by the events.
    add_bounded_timezones_before_events(cal)

    ics_bytes = cal.to_ical()
    published = sum(1 for c in cal.subcomponents if c.name == "VEVENT")
    log.info("Converted to %d VEVENT(s) (%d fetched events; birthdays/memos are skipped).",
             published, fetched)
    return ics_bytes, fetched, published


def _apply_calendar_headers(cal: ICalendar, feed_name: str) -> None:
    """Set stable top-level calendar properties for a subscription feed."""
    if "prodid" in cal:
        del cal["prodid"]
    cal.add("prodid", PRODID)
    if "version" not in cal:
        cal.add("version", "2.0")
    if "calscale" not in cal:
        cal.add("calscale", "GREGORIAN")
    cal.add("x-wr-calname", feed_name)
    cal.add("name", feed_name)
    # Hint subscribers to re-poll ~twice a day (advisory only). REFRESH-INTERVAL
    # is the RFC 7986 property; X-PUBLISHED-TTL is the older Outlook/Google hint,
    # conventionally a bare ISO-8601 duration string (PT12H).
    cal.add("refresh-interval", REFRESH_INTERVAL, parameters={"VALUE": "DURATION"})
    cal.add("x-published-ttl", vDuration(REFRESH_INTERVAL).to_ical().decode("ascii"))


def _ensure_date_value_params(cal: ICalendar) -> None:
    """Stamp VALUE=DATE on any date-valued (all-day) DTSTART/DTEND/RECURRENCE-ID.

    Detection has to work at two stages: on the freshly-built calendar the
    property is a ``vDate`` whose ``.dt`` is (confusingly) a datetime, while a
    re-parsed property is a plain ``date``. Handle both.
    """
    for ev in cal.walk("VEVENT"):
        for name in ("DTSTART", "DTEND", "RECURRENCE-ID"):
            prop = ev.get(name)
            if prop is None:
                continue
            value = getattr(prop, "dt", None)
            is_date_only = (isinstance(prop, vDate) and not isinstance(prop, vDatetime)) or (
                isinstance(value, date) and not isinstance(value, datetime)
            )
            if is_date_only:
                prop.params["VALUE"] = "DATE"


# --------------------------------------------------------------------------- #
# Validation + guardrails
# --------------------------------------------------------------------------- #
def validate_ics(ics_bytes: bytes, allow_empty: bool) -> int:
    """Re-parse the generated feed and assert it is structurally sound.

    Returns the VEVENT count. Raises SyncError on anything that would make the
    feed invalid or unsafe to publish.
    """
    try:
        cal = ICalendar.from_ical(ics_bytes)
    except Exception as exc:
        raise SyncError(f"Generated .ics failed to re-parse (invalid RFC 5545): {exc}") from exc

    vevents = list(cal.walk("VEVENT"))
    if not vevents and not allow_empty:
        raise SyncError(
            "Generated feed contains 0 events — refusing to publish. "
            "If the calendar is genuinely empty, set ALLOW_EMPTY=1."
        )

    uids: list[str] = []
    for ev in vevents:
        uid = str(ev.get("uid") or "").strip()
        if not uid:
            raise SyncError("An event has an empty UID — refusing (would break stable sync).")
        dtstart = ev.get("dtstart")
        if dtstart is None:
            raise SyncError(f"Event {uid} has no DTSTART — refusing to publish.")
        # All-day (DATE) starts must carry VALUE=DATE or strict parsers misread them.
        value = getattr(dtstart, "dt", None)
        if (
            isinstance(value, date)
            and not isinstance(value, datetime)
            and dtstart.params.get("VALUE") != "DATE"
        ):
            raise SyncError(f"Event {uid} is an all-day event missing VALUE=DATE — refusing.")
        uids.append(uid)

    dupes = [uid for uid, n in Counter(uids).items() if n > 1]
    if dupes:
        raise SyncError(
            f"Duplicate UIDs detected ({len(dupes)}), e.g. {dupes[:3]} — "
            "publishing would create duplicate events downstream. Refusing."
        )
    return len(vevents)


def fetch_baseline_vevent_count(url: str | None, timeout: int = 20) -> int | None:
    """Count VEVENTs in the currently-published feed, for the drop-guard.

    Returns None (and skips the guard) on first run / 404 / any fetch error —
    the guard is a safety net, never a hard dependency.
    """
    if not url:
        return None
    try:
        resp = requests.get(url, timeout=timeout)
        if resp.status_code == 404:
            log.info("No existing feed at the baseline URL (first run?). Drop-guard skipped.")
            return None
        resp.raise_for_status()
        cal = ICalendar.from_ical(resp.content)
        count = sum(1 for _ in cal.walk("VEVENT"))
        log.info("Baseline (currently-published) feed has %d event(s).", count)
        return count
    except Exception as exc:  # non-fatal by design
        log.warning("Could not read the baseline feed for the drop-guard (ignoring): %s", exc)
        return None


def enforce_drop_guard(new_count: int, baseline: int | None, max_drop_ratio: float) -> None:
    """Fail loudly if the event count collapsed vs the last good feed."""
    if baseline is None or baseline < MIN_BASELINE_FOR_DROP_GUARD:
        return
    if max_drop_ratio >= 1.0:
        return
    floor = int(baseline * (1.0 - max_drop_ratio))
    if new_count < floor:
        raise SyncError(
            f"Event count dropped from {baseline} to {new_count} (floor {floor}, "
            f"max drop {int(max_drop_ratio * 100)}%). This looks like partial pagination "
            "or an API change. Refusing to overwrite the good feed. Set MAX_DROP_RATIO=1 "
            "to override if the drop is real."
        )


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def write_outputs(ics_bytes: bytes, out_path: Path, status: dict) -> None:
    """Write the feed and a sidecar status.json next to it (atomic-ish)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)

    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_bytes(ics_bytes)
    tmp.replace(out_path)

    status_path = out_path.with_name("status.json")
    status_path.write_text(json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    log.info("Wrote feed -> %s (%d bytes).", out_path, len(ics_bytes))
    log.info("Wrote status -> %s", status_path)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=TOOL_NAME,
        description="Export one TimeTree calendar to a stable, valid .ics feed.",
    )
    parser.add_argument(
        "-o", "--output",
        default=os.environ.get("FEED_OUTPUT", "_site/calendar.ics"),
        help="Path to write the .ics feed (default: _site/calendar.ics or $FEED_OUTPUT).",
    )
    parser.add_argument(
        "--name",
        default=os.environ.get("FEED_NAME", "TimeTree"),
        help="Display name for the calendar feed (X-WR-CALNAME).",
    )
    parser.add_argument(
        "--calendar",
        default=os.environ.get("TIMETREE_CALENDAR_ID"),
        help="Calendar(s) to export: numeric id, alias code, or exact name. "
             "Accepts a comma-separated list to merge several "
             "(default: $TIMETREE_CALENDAR_ID).",
    )
    parser.add_argument(
        "--all-calendars", action="store_true", default=_env_flag("TIMETREE_ALL_CALENDARS"),
        help="Export ALL of the account's active calendars, merged into one feed "
             "(default from $TIMETREE_ALL_CALENDARS). Overrides --calendar.",
    )
    parser.add_argument(
        "--list-calendars", action="store_true",
        help="Authenticate, print the available calendars (id/code/name), and exit.",
    )
    parser.add_argument(
        "--baseline-url",
        default=os.environ.get("BASELINE_URL"),
        help="URL of the currently-published feed, for the count-drop guard "
             "(default: $BASELINE_URL). Optional.",
    )
    parser.add_argument(
        "--max-drop-ratio", type=float,
        default=float(os.environ.get("MAX_DROP_RATIO") or "0.5"),
        help="Fail if the event count drops by more than this fraction vs baseline "
             "(default 0.5; set 1 to disable).",
    )
    parser.add_argument(
        "--allow-empty", action="store_true", default=_env_flag("ALLOW_EMPTY"),
        help="Permit publishing a feed with 0 events (default: refuse).",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose (DEBUG) logging.")
    return parser.parse_args(argv)


def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    # The library logs raw event samples at DEBUG; keep it at INFO unless asked.
    logging.getLogger("timetree_exporter").setLevel(level)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging(args.verbose)

    started = dt.datetime.now(dt.timezone.utc)
    log.info("%s starting at %s (UTC).", TOOL_NAME, started.isoformat())

    session_id = authenticate()
    api = TimeTreeCalendar(session_id)

    if args.list_calendars:
        print(_format_calendar_listing(load_calendars(api)))
        return 0

    if args.all_calendars:
        calendars = select_calendars(api, all_calendars=True)
    else:
        if not args.calendar:
            raise SyncError(
                "No calendar selected. Set TIMETREE_CALENDAR_ID (or pass --calendar), "
                "or set TIMETREE_ALL_CALENDARS=1 to export every calendar. "
                "Run with --list-calendars to see the options."
            )
        selectors = str(args.calendar).split(",")
        calendars = select_calendars(api, selectors=selectors)

    ics_bytes, fetched, published = build_feed(calendars, args.name)
    validated = validate_ics(ics_bytes, allow_empty=args.allow_empty)

    baseline = fetch_baseline_vevent_count(args.baseline_url)
    enforce_drop_guard(validated, baseline, args.max_drop_ratio)

    status = {
        "tool": TOOL_NAME,
        "generated_at_utc": started.isoformat(),
        "calendars": [{"name": c.name, "id": str(c.id)} for c in calendars],
        "calendar_count": len(calendars),
        "fetched_events": fetched,
        "published_events": validated,
        "baseline_events": baseline,
        "timetree_exporter_version": _tool_version(),
        "icalendar_version": _lib_version("icalendar"),
        "run_url": _run_url(),
    }
    write_outputs(ics_bytes, Path(args.output), status)

    log.info(
        "SUCCESS: %d calendar(s) fetched=%d published=%d baseline=%s",
        len(calendars), fetched, validated, baseline,
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SyncError as exc:
        log.error("SYNC FAILED: %s", exc)
        sys.exit(1)
    except KeyboardInterrupt:  # pragma: no cover
        log.error("Interrupted.")
        sys.exit(130)
    except Exception as exc:  # noqa: BLE001 - top-level loud failure
        log.exception("SYNC FAILED (unexpected): %s", exc)
        sys.exit(1)
