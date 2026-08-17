# TimeTree → Skylight calendar sync

A scheduled, **one-way** pipeline that exports one TimeTree calendar and
publishes it as a stable, valid `.ics` feed at a fixed public URL — the URL you
subscribe to **once** from the Skylight app. After that, Skylight re-pulls the
feed on its own; this pipeline just keeps the feed fresh, unattended, twice a day.

```
TimeTree  ──(fetch, paginated)──►  sync.py  ──(convert + validate)──►  calendar.ics
                                                                            │
                                              GitHub Pages (fixed URL) ◄──(deploy)
                                                                            │
                                                                    Skylight subscribes
```

- **No app to run, no server to maintain.** GitHub Actions runs the cron;
  GitHub Pages hosts the feed. Both are free for a public repo.
- **Fails loud, never silently.** A broken/partial run refuses to publish and
  leaves the last good feed in place, and opens a GitHub issue + emails you.
- **Stable events.** Each event's UID is derived deterministically from its
  TimeTree id, so re-publishing never creates duplicate/"new" events in Skylight.

---

## Why these choices

**Language: Python.** The only actively-maintained client for TimeTree's
unofficial API is [`timetree-exporter`](https://github.com/eoleedi/TimeTree-Exporter)
(Python; v0.8.0 was released **July 2026**). It already handles the fragile
parts correctly — login, the chunked ~300-events-per-request pagination, the
event model, and RFC-5545 conversion including `RRULE`, all-day `VALUE=DATE`,
and bounded `VTIMEZONE` blocks. Re-implementing that reverse-engineered layer in
another language would duplicate a maintained project and break far more often.
`sync.py` builds **on** it and adds the orchestration, hardening, and publishing.

**Hosting: GitHub Actions + GitHub Pages.** No infrastructure you have to own.
The feed **must** be publicly reachable (Skylight requires an unauthenticated
URL), so a public repo is the simplest fit. Your TimeTree password is **not** in
the repo — it lives only in encrypted Actions Secrets. The generated `.ics` is
**never committed to git**; it is deployed straight to the Pages site, so your
event data isn't in the repo history.

**URL secrecy.** The feed lives under an unguessable slug
(`/feed/<random>/calendar.ics`) so it isn't casually discoverable or crawled.
Anyone with the exact URL can read it — that's inherent to any ICS subscription
feed — so treat the URL like a password.

---

## What you need

- A GitHub account (free is fine).
- Your TimeTree email + password (or a session token — see below).
- The Skylight app, for the one-time subscribe step.

---

## Setup (one time)

### Fastest path: `bootstrap.sh`

If you have the [GitHub CLI](https://cli.github.com/) installed, one script does
almost all of this for you — creates the repo, pushes, enables Pages, sets the
secrets (prompting for your password securely), finds your calendar, and starts
the first sync:

```bash
gh auth login --scopes 'repo,workflow'   # once, if you haven't
cd timetree-skylight-sync
./bootstrap.sh
```

Answer the prompts (email, password, calendar name, then the calendar id it
shows you). At the end it prints your feed URL — subscribe to that in Skylight
and you're done. The manual steps below are the same thing, spelled out, if you
prefer clicking or don't want to use `gh`.

---

### 1. Create the repo and push these files

Create a **new, empty, public** GitHub repo (e.g. `timetree-skylight-sync`) —
**not** connected to any other project. Then, from this folder:

```bash
git init
git add .
git commit -m "TimeTree → Skylight sync"
git branch -M main
git remote add origin https://github.com/<you>/timetree-skylight-sync.git
git push -u origin main
```

### 2. Turn on GitHub Pages (Actions source)

Repo **Settings → Pages → Build and deployment → Source: GitHub Actions**.
(You don't pick a branch; the workflow deploys the site.)

### 3. Add your secrets and variables

Repo **Settings → Secrets and variables → Actions**.

**Secrets** (encrypted, never shown in logs):

| Secret | Required | What it is |
| --- | --- | --- |
| `TIMETREE_EMAIL` | yes\* | Your TimeTree login email |
| `TIMETREE_PASSWORD` | yes\* | Your TimeTree password |
| `TIMETREE_SESSION_ID` | optional | A reused `_session_id` token instead of email/password (see [Session token](#using-a-session-token-instead-of-a-password)) |
| `TIMETREE_CALENDAR_ID` | yes | Which calendar to export (get it in step 4) |
| `FEED_SLUG` | recommended | The unguessable path segment. Generate: `python -c "import uuid;print(uuid.uuid4().hex)"` |

\* Provide **either** `TIMETREE_SESSION_ID` **or** both `TIMETREE_EMAIL` +
`TIMETREE_PASSWORD`.

**Variables** (non-secret):

| Variable | Required | What it is |
| --- | --- | --- |
| `FEED_NAME` | recommended | Display name Skylight shows (e.g. `Our Family`) |
| `PAGES_BASE_URL` | after first deploy | Your Pages base, e.g. `https://<you>.github.io/timetree-skylight-sync` — enables the drop-guard |
| `MAX_DROP_RATIO` | optional | Fail if event count drops more than this fraction vs the live feed (default `0.5`) |

### 4. Find your calendar id

Actions tab → **List TimeTree calendars** → **Run workflow**. Open the run log;
it prints each calendar as `id=… code=… name=…`. Copy the **`id`** of the
calendar you want and set it as the `TIMETREE_CALENDAR_ID` secret.

> If you have several shared calendars, this is where you pick the right one.

### 5. Run the sync once, by hand

Actions tab → **TimeTree to Skylight sync** → **Run workflow**. It should end
green. The log prints the event count and a `SUCCESS:` line. If it fails, it
tells you why (bad credentials, wrong calendar, empty feed, etc.) and publishes
nothing.

### 6. Get your feed URL

It is:

```
https://<you>.github.io/<repo>/feed/<FEED_SLUG>/calendar.ics
```

Open it in a browser — it should download/serve an `.ics` starting with
`BEGIN:VCALENDAR`. Now set the `PAGES_BASE_URL` variable (step 3) to
`https://<you>.github.io/<repo>` so future runs can compare against the live
feed.

### 7. Validate the feed BEFORE subscribing

The pipeline already self-validates every run and **refuses to publish** an
invalid or suspiciously-empty feed. To double-check independently:

```bash
# Structural RFC-5545 parse (fails loudly if malformed):
curl -sL "https://<you>.github.io/<repo>/feed/<slug>/calendar.ics" -o feed.ics
python -c "from icalendar import Calendar; c=Calendar.from_ical(open('feed.ics','rb').read()); print('events:', sum(1 for _ in c.walk('VEVENT')))"
```

Sanity-check that the event count matches roughly what's in TimeTree, and spot
a known all-day event and a known recurring event look right. You can also paste
the URL into a third-party iCalendar validator (e.g. an "ICS validator" web tool)
if you have one you trust.

### 8. Subscribe in Skylight (the one-time step)

In the Skylight app: **Sync new calendar → Calendar URL** (also labelled
"Other" / "iCal URL" in some versions) → paste the feed URL → save. Skylight
will fetch it and refresh on its own schedule from then on. **You never touch
this again** unless the URL changes (it won't, as long as `FEED_SLUG` and the
repo name stay the same).

Done. The cron keeps the feed current twice a day.

---

## Scheduling

Defined in `.github/workflows/sync.yml`:

```yaml
on:
  schedule:
    - cron: "0 6 * * *"    # 06:00 UTC
    - cron: "0 18 * * *"   # 18:00 UTC
```

GitHub cron is **always UTC**. To change cadence, edit these lines (e.g. one
line for once-daily, four for every 6 hours). You can also trigger a run any
time from the Actions tab. Note: scheduled Actions can be delayed several
minutes under GitHub load, and GitHub disables schedules on repos with **no
activity for 60 days** — a manual run (or any push) re-arms them.

---

## Failure notifications

Two signals, no extra infrastructure:

1. **Email** — GitHub emails you automatically when a scheduled run fails
   (make sure Actions failure notifications are on in your GitHub
   notification settings).
2. **A tracking issue** — on failure the workflow opens (or comments on) an
   issue titled *"⚠️ TimeTree → Skylight sync failed"* with a link to the run.

On failure the feed is **not** overwritten: the deploy step only runs when the
generate+validate step succeeds, so Skylight keeps seeing the last good feed.

---

## If the feed stops updating

Work down this list:

1. **Is the last run green?** Actions tab → latest *TimeTree to Skylight sync*
   run. A red run = the cause is in the log. A missing run = the schedule was
   disabled (60-day inactivity) → run it manually to re-arm.
2. **Auth failure?** The log says login was rejected or rate-limited. TimeTree
   may have forced a password change, or is throttling logins → rotate the
   secret, or switch to a `TIMETREE_SESSION_ID` token.
3. **"0 events" / big drop refused?** The guardrails caught a suspiciously
   empty or shrunken result (often a TimeTree API shape change or partial
   pagination). The old feed is intact. If the drop is *real* (you deleted a lot
   of events), set `MAX_DROP_RATIO` to `1` for one run, then back.
4. **API shape changed?** `timetree-exporter` talks to an unofficial API. If
   TimeTree changed it, bump the pinned version in `requirements.txt` once
   upstream ships a fix (`pip index versions timetree-exporter`).
5. **Feed serves but Skylight is stale?** Confirm the URL still returns
   `BEGIN:VCALENDAR` in a browser. Skylight caches; give it a few hours, or
   re-add the URL. Confirm you didn't change `FEED_SLUG` or the repo name (that
   changes the URL and orphans Skylight's subscription).
6. **Pages down / 404?** Settings → Pages should show the site as deployed with
   Source = GitHub Actions.

---

## Using a session token instead of a password

Logging in on every scheduled run is what most often trips TimeTree's
rate-limiting. To avoid it, extract a session token once and set it as
`TIMETREE_SESSION_ID` (then you can drop the email/password secrets):

1. Log in to `https://timetreeapp.com` in a browser.
2. DevTools → Application/Storage → Cookies → copy the value of `_session_id`.
3. Set it as the `TIMETREE_SESSION_ID` secret.

Tokens expire eventually; when the run reports an auth failure, refresh it. If
you'd rather not babysit a token, keep using email/password — twice-daily logins
are usually fine.

---

## Risks & the honest caveats

- **The unofficial API is the fragile part.** TimeTree has no public API (the
  official one was shut down Dec 2023). Everything here rides on a
  reverse-engineered endpoint that can change without notice. The guardrails
  turn "silent breakage" into "loud, non-destructive failure" — but they can't
  prevent breakage. This is the single most likely thing to need attention.
- **Don't get your account throttled.** Twice a day is gentle. Don't crank the
  cron to every few minutes — repeated logins/large pulls can get you
  rate-limited or temporarily blocked.
- **Bounded VTIMEZONE.** The feed embeds `VTIMEZONE` blocks bounded to the date
  range of your events (upstream keeps them small so big calendars still import
  into Google). Virtually all clients — including Google, Apple, and Skylight —
  resolve `TZID` names against their own IANA database anyway, so this is belt
  and suspenders. The only theoretical edge case is a client that relies *solely*
  on the embedded block for a recurring event whose occurrences cross a DST
  boundary far outside the event's own date range; in practice this doesn't bite
  near-term family calendars. If you ever see an off-by-an-hour recurring event
  in Skylight, that's the thing to suspect.
- **All-day end dates.** TimeTree's all-day `end` is treated as the last day and
  emitted as an RFC-correct exclusive `DTEND` (+1 day). Confirm one known
  multi-day all-day event looks right in Skylight after your first subscribe —
  this is the one area worth eyeballing against live data.
- **Public URL = public data.** Anyone with the URL sees the events. The slug
  keeps it unlisted; it is not access control. Don't put the URL anywhere public.
- **One-way only.** Edits in Skylight do **not** flow back to TimeTree. TimeTree
  is the source of truth.
- **Birthday and memo entries are skipped** (TimeTree models them as
  non-calendar items). If you want them, that's a code change in the upstream
  formatter.

---

## Local testing (optional)

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # fill in real values; NEVER commit .env
set -a; . ./.env; set +a
python sync.py --list-calendars           # discover calendar ids
python sync.py --output _site/feed/$FEED_SLUG/calendar.ics --name "$FEED_NAME"
python test_offline.py                    # offline conversion/guardrail tests (no network)
```

---

## Files

| File | Purpose |
| --- | --- |
| `sync.py` | Fetch → convert → validate → guardrails → write the feed |
| `bootstrap.sh` | One-command setup (repo, Pages, secrets, first run) via `gh` |
| `requirements.txt` | Pinned dependencies |
| `.github/workflows/sync.yml` | Twice-daily cron: generate, validate, deploy to Pages, notify on failure |
| `.github/workflows/list-calendars.yml` | Manual helper to discover calendar ids |
| `test_offline.py` | Offline tests for the conversion + guardrails |
| `example-output.ics` | A sample of the generated feed |
| `.env.example` | Template for local runs |

Not affiliated with or endorsed by TimeTree, Inc. or Skylight. See `LICENSE`.
