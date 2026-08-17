"""Offline smoke test for the conversion + validation path (no network/credentials).

Feeds synthetic TimeTree-shaped event dicts through the same code sync.py uses
and asserts the RFC 5545 output is correct and the guardrails fire.
"""
import datetime as dt
from zoneinfo import ZoneInfo

from icalendar import Calendar as ICalendar

import sync
from timetree_exporter.exporter import build_single_calendar


def ms(y, mo, d, h=0, mi=0, tz="UTC"):
    return int(dt.datetime(y, mo, d, h, mi, tzinfo=ZoneInfo(tz)).timestamp() * 1000)


NY = "America/New_York"

EVENTS = [
    {  # 1. timed event with a timezone + alert
        "uuid": "evt-timed-0001", "title": "Soccer practice",
        "created_at": ms(2026, 1, 1), "updated_at": ms(2026, 1, 2),
        "start_at": ms(2026, 8, 12, 14, 0, NY), "end_at": ms(2026, 8, 12, 15, 30, NY),
        "start_timezone": NY, "end_timezone": NY, "all_day": False,
        "alerts": [10], "recurrences": None, "note": "Bring water",
        "location": "Field 3", "location_lat": None, "location_lon": None, "url": "",
        "parent_id": None, "recurring_uuid": None, "type": 0, "category": 1, "label_id": None,
    },
    {  # 2. single-day all-day event -> DATE, exclusive DTEND (+1 day)
        "uuid": "evt-allday-0002", "title": "Trip",
        "created_at": ms(2026, 1, 1), "updated_at": ms(2026, 1, 2),
        "start_at": ms(2026, 8, 20, 0, 0, NY), "end_at": ms(2026, 8, 20, 0, 0, NY),
        "start_timezone": NY, "end_timezone": NY, "all_day": True,
        "alerts": None, "recurrences": None, "note": "", "location": "",
        "location_lat": None, "location_lon": None, "url": "",
        "parent_id": None, "recurring_uuid": None, "type": 0, "category": 1, "label_id": None,
    },
    {  # 3. recurring weekly event
        "uuid": "evt-recur-0003", "title": "Team standup",
        "created_at": ms(2026, 1, 1), "updated_at": ms(2026, 1, 2),
        "start_at": ms(2026, 8, 10, 9, 0, NY), "end_at": ms(2026, 8, 10, 9, 15, NY),
        "start_timezone": NY, "end_timezone": NY, "all_day": False,
        "alerts": None, "recurrences": ["RRULE:FREQ=WEEKLY;COUNT=5;BYDAY=MO"],
        "note": "", "location": "", "location_lat": None, "location_lon": None, "url": "",
        "parent_id": None, "recurring_uuid": None, "type": 0, "category": 1, "label_id": None,
    },
    {  # 4. birthday -> skipped
        "uuid": "evt-bday-0004", "title": "Grandma birthday",
        "created_at": ms(2026, 1, 1), "updated_at": ms(2026, 1, 2),
        "start_at": ms(2026, 9, 1, 0, 0, NY), "end_at": ms(2026, 9, 2, 0, 0, NY),
        "start_timezone": NY, "end_timezone": NY, "all_day": True,
        "alerts": None, "recurrences": None, "note": "", "location": "",
        "location_lat": None, "location_lon": None, "url": "",
        "parent_id": None, "recurring_uuid": None, "type": 1, "category": 1, "label_id": None,
    },
    {  # 5. memo -> skipped
        "uuid": "evt-memo-0005", "title": "Grocery list",
        "created_at": ms(2026, 1, 1), "updated_at": ms(2026, 1, 2),
        "start_at": ms(2026, 8, 15, 12, 0, NY), "end_at": ms(2026, 8, 15, 12, 0, NY),
        "start_timezone": NY, "end_timezone": NY, "all_day": False,
        "alerts": None, "recurrences": None, "note": "", "location": "",
        "location_lat": None, "location_lon": None, "url": "",
        "parent_id": None, "recurring_uuid": None, "type": 0, "category": 2, "label_id": None,
    },
]


def build(events):
    # Mirror sync.build_feed's assembly order exactly.
    cal = build_single_calendar(events, {})
    sync._apply_calendar_headers(cal, "Family")
    sync._ensure_date_value_params(cal)
    sync.add_bounded_timezones_before_events(cal)
    return cal.to_ical()


def test_conversion_valid():
    ics = build(EVENTS)
    n = sync.validate_ics(ics, allow_empty=False)
    assert n == 3, f"expected 3 VEVENTs (birthday+memo skipped), got {n}"

    text = ics.decode("utf-8")
    parsed = ICalendar.from_ical(ics)

    uids = {str(e.get("uid")) for e in parsed.walk("VEVENT")}
    assert uids == {"evt-timed-0001", "evt-allday-0002", "evt-recur-0003"}, uids

    assert "BEGIN:VTIMEZONE" in text and "TZID:America/New_York" in text, "missing VTIMEZONE"
    assert "PRODID:-//timetree-skylight-sync//TimeTree to Skylight//EN" in text
    assert "X-WR-CALNAME:Family" in text
    assert "REFRESH-INTERVAL;VALUE=DURATION:PT12H" in text
    assert "X-PUBLISHED-TTL:PT12H" in text

    # all-day: DATE value + exclusive DTEND (single day 08-20 -> exclusive 08-21)
    assert "DTSTART;VALUE=DATE:20260820" in text, "all-day DTSTART not a VALUE=DATE"
    assert "DTEND;VALUE=DATE:20260821" in text, "all-day DTEND not exclusive next-day VALUE=DATE"

    # timed event carries a TZID (not floating)
    assert "DTSTART;TZID=America/New_York:20260812T140000" in text

    # recurrence passthrough
    assert "RRULE:FREQ=WEEKLY;COUNT=5;BYDAY=MO" in text
    print("PASS: conversion valid (3 VEVENTs, VTIMEZONE, all-day DATE, TZID, RRULE, stable UIDs)")


def test_determinism():
    a = build(EVENTS)
    b = build(EVENTS)

    def uids(x):
        return sorted(str(e.get("uid")) for e in ICalendar.from_ical(x).walk("VEVENT"))

    assert uids(a) == uids(b), "UID set changed between runs"

    def strip_dtstamp(x):
        return "\n".join(l for l in x.decode().splitlines() if not l.startswith("DTSTAMP"))

    assert strip_dtstamp(a) == strip_dtstamp(b), "feed not deterministic apart from DTSTAMP"
    print("PASS: deterministic UIDs and stable serialization across runs")


def test_empty_refused():
    ics = build([])
    try:
        sync.validate_ics(ics, allow_empty=False)
    except sync.SyncError:
        print("PASS: empty feed refused (fail-loud)")
    else:
        raise AssertionError("empty feed should have been refused")
    # allow_empty override works
    assert sync.validate_ics(ics, allow_empty=True) == 0
    print("PASS: ALLOW_EMPTY override permits an empty feed")


def test_duplicate_uid_refused():
    dupe = dict(EVENTS[0])
    ics = build([EVENTS[0], dupe])
    try:
        sync.validate_ics(ics, allow_empty=False)
    except sync.SyncError as e:
        assert "Duplicate UID" in str(e)
        print("PASS: duplicate UID refused")
    else:
        raise AssertionError("duplicate UID should have been refused")


def test_drop_guard():
    # baseline 100 -> new 40 with 50% max drop should fail (floor 50)
    try:
        sync.enforce_drop_guard(40, 100, 0.5)
    except sync.SyncError:
        print("PASS: drop-guard fires on a large count drop")
    else:
        raise AssertionError("drop-guard should have fired")
    # small, tolerated drop passes
    sync.enforce_drop_guard(60, 100, 0.5)
    # disabled at ratio 1
    sync.enforce_drop_guard(0, 100, 1.0)
    # tiny baseline ignored
    sync.enforce_drop_guard(0, 3, 0.5)
    print("PASS: drop-guard tolerances behave correctly")


if __name__ == "__main__":
    test_conversion_valid()
    test_determinism()
    test_empty_refused()
    test_duplicate_uid_refused()
    test_drop_guard()
    print("\nALL OFFLINE TESTS PASSED")
