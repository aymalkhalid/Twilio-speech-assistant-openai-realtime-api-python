"""
Quick test for Google Calendar integration.
Run from project root: python -m tests.test_google_calendar
Or: python tests/test_google_calendar.py
"""
import time

from dotenv import load_dotenv

load_dotenv()

from services import google_calendar_booking_service as gcs
from services.google_calendar_booking_service import (
    get_availability,
    is_booking_enabled,
    book_appointment,
    list_my_bookings,
    delete_booking,
    edit_booking,
    invalidate_availability_cache,
    _normalize_phone,
)
from config import Config


def main():
    print("Checking if booking is enabled...")
    if not is_booking_enabled():
        print("FAIL: Booking not enabled. Check GOOGLE_CALENDAR_CREDENTIALS_JSON and GOOGLE_CALENDAR_ID in .env")
        return
    print("OK: Credentials and calendar ID loaded.\n")

    print("Fetching availability (full week, grouped by day and morning/afternoon/evening)...")
    result = get_availability(days_ahead=7)
    by_day = result.get("by_day") or {}
    slots_flat = result.get("slots_flat") or []
    if not by_day:
        print("No slots returned (calendar may be busy, or check calendar sharing with the service account).")
    else:
        for date_str, day_data in sorted(by_day.items()):
            print(f"  {day_data.get('date_display', date_str)}")
            for bucket in ("morning", "afternoon", "evening"):
                slot_list = day_data.get(bucket) or []
                if slot_list:
                    print(f"    {bucket}: " + ", ".join(s.get("display", s.get("start", "")) for s in slot_list))
        print(f"\n  Total slots: {len(slots_flat)}")
    print("\nDone. If you see slots above, integration is working.")


# --- Edge-case tests (no live calendar required for these) ---


def test_phone_display_for_calendar_us():
    """US numbers show as (XXX) XXX-XXXX in calendar description helper."""
    assert gcs._phone_display_for_calendar("+1 (218) 555-1234") == "(218) 555-1234"
    assert gcs._phone_display_for_calendar("2185551234") == "(218) 555-1234"
    assert gcs._phone_display_for_calendar("") == "—"


def test_parse_booking_description_for_reschedule():
    """Reschedule path can recover email, notes, and service type from our booking template."""
    desc = gcs._build_booking_calendar_description(
        contact_name="Jane",
        contact_phone="+12185551234",
        contact_email="j@example.com",
        visit_summary="Deep clean",
        when_local_display="Mon at 9 AM",
        when_local_long="Monday at 9:00 AM",
        tz_name="America/Chicago",
        duration_minutes=60,
        company="Acme",
        category_display="cleaning",
        rescheduled=False,
    )
    email, notes, svc = gcs._parse_booking_description_for_reschedule(desc)
    assert email == "j@example.com"
    assert notes == "Deep clean"
    assert svc == "cleaning"
    assert gcs._parse_booking_description_for_reschedule("Contact: a, b") == (None, None, None)


def test_build_booking_calendar_summary_and_description():
    """Booking event title and body use structured company / caller / notes."""
    s = gcs._build_booking_calendar_summary(
        "Jane Doe", "Deep clean kitchen", "Acme Clean", "cleaning service"
    )
    assert "Acme Clean" in s and "Jane Doe" in s and "Deep clean" in s
    s2 = gcs._build_booking_calendar_summary("Bob", None, "Co", None)
    assert "Co" in s2 and "Bob" in s2
    desc = gcs._build_booking_calendar_description(
        contact_name="Jane",
        contact_phone="+12185551234",
        contact_email="j@example.com",
        visit_summary="Move-out clean",
        when_local_display="Mon Jan 1 at 9:00 AM",
        when_local_long="Monday, January 01 at 9:00 AM",
        tz_name="America/Chicago",
        duration_minutes=60,
        company="Acme",
        category_display="cleaning",
    )
    assert "Jane" in desc and "(218) 555-1234" in desc and "j@example.com" in desc
    assert "Move-out" in desc and "America/Chicago" in desc and "60 minutes" in desc
    desc2 = gcs._build_booking_calendar_description(
        contact_name="Jane",
        contact_phone="2185551234",
        contact_email=None,
        visit_summary=None,
        when_local_display="Tue 2 PM",
        when_local_long="Tuesday at 2:00 PM",
        tz_name="America/Chicago",
        duration_minutes=30,
        company="Acme",
        category_display=None,
        rescheduled=True,
    )
    assert desc2.startswith("Rescheduled via voice agent")


def test_normalize_phone_us_canonical():
    """E.164 +1 and formatted US number must normalize to same 10 digits so list_my_bookings finds events."""
    assert _normalize_phone("+12185953061") == "2185953061"
    assert _normalize_phone("218-595-3061") == "2185953061"
    assert _normalize_phone("+12185953061") == _normalize_phone("218-595-3061")
    assert _normalize_phone("1-218-595-3061") == "2185953061"
    # Non-US 11 digits: keep as-is (no leading 1)
    assert _normalize_phone("44123456789") == "44123456789"
    # 10 digits unchanged
    assert _normalize_phone("2185953061") == "2185953061"


def test_list_my_bookings_empty_phone():
    """list_my_bookings with empty phone returns [] (no crash)."""
    out = list_my_bookings("")
    assert out == [], "list_my_bookings('') should return []"
    out = list_my_bookings("   ")
    assert out == [], "list_my_bookings('   ') should return []"


def test_list_my_bookings_returns_context_for_disambiguation(monkeypatch):
    """list_my_bookings should expose stored caller name and visit notes for exact-booking clarification."""
    description = gcs._build_booking_calendar_description(
        contact_name="Amel",
        contact_phone="+12185953061",
        contact_email=None,
        visit_summary="Standard residential cleaning",
        when_local_display="Mon Apr 20 at 08:00 AM",
        when_local_long="Monday, April 20 at 08:00 AM",
        tz_name="America/Los_Angeles",
        duration_minutes=60,
        company="Prestige Cleaning Services",
        category_display="cleaning",
        rescheduled=False,
    )
    item = {
        "id": "evt_morning_1",
        "summary": "Prestige Cleaning Services — Amel: Standard residential cleaning",
        "description": description,
        "start": {"dateTime": "2026-04-20T15:00:00Z"},
        "end": {"dateTime": "2026-04-20T16:00:00Z"},
        "extendedProperties": {
            "private": {
                "caller_phone": "2185953061",
                "caller_name": "Amel",
            }
        },
    }
    monkeypatch.setattr(gcs, "_get_calendar_service", lambda: (_FakeCalendarService([item]), "calendar_id"))

    out = list_my_bookings("+12185953061", "Emil")

    assert len(out) == 1
    assert out[0]["event_id"] == "evt_morning_1"
    assert out[0]["caller_name"] == "Amel"
    assert out[0]["visit_summary"] == "Standard residential cleaning"
    assert out[0]["service_type"] == "cleaning"


def test_delete_booking_missing_event_id():
    """delete_booking with empty event_id returns failure."""
    r = delete_booking("", "1234567890")
    assert isinstance(r, dict) and r.get("success") is False
    assert "message" in r
    assert "not found" in r.get("message", "").lower() or "not configured" in r.get("message", "").lower()


def test_edit_booking_missing_args():
    """edit_booking with empty event_id or new_slot returns failure."""
    r = edit_booking("", "2026-06-01T14:00:00Z", "1234567890")
    assert isinstance(r, dict) and r.get("success") is False
    assert r.get("confirmed_slot") is None
    assert "message" in r

    r2 = edit_booking("some_id", "", "1234567890")
    assert isinstance(r2, dict) and r2.get("success") is False
    assert r2.get("confirmed_slot") is None


def test_invalidate_availability_cache_clears_entries():
    """After calendar writes, cache must clear; unit-test the helper directly (no live Calendar)."""
    gcs.reset_availability_cache_metrics()
    k = (7, "", "mon-fri")
    gcs._availability_cache[k] = ({"by_day": {}, "slots_flat": []}, 1e12)
    invalidate_availability_cache(reason="test")
    assert len(gcs._availability_cache) == 0
    m = gcs.get_availability_cache_metrics()
    assert m["invalidation_call"] == 1
    assert m["invalidation_entries_cleared_sum"] == 1


def test_invalidate_availability_cache_metrics_when_empty():
    """Invalidation always bumps invalidation_call; entries_cleared sum only when something was stored."""
    gcs.reset_availability_cache_metrics()
    gcs._availability_cache.clear()
    invalidate_availability_cache(reason="noop")
    m = gcs.get_availability_cache_metrics()
    assert m["invalidation_call"] == 1
    assert m["invalidation_entries_cleared_sum"] == 0


def test_availability_cache_metrics_hit_seeded(monkeypatch):
    """Seeded valid cache entry → get_availability returns it without miss (no Calendar call on hit path)."""
    monkeypatch.setenv("AVAILABILITY_CACHE_ENABLED", "true")
    gcs.reset_availability_cache_metrics()
    profile = gcs._availability_cache_profile_key()
    k = (7, "", profile)
    gcs._availability_cache[k] = ({"by_day": {}, "slots_flat": []}, time.time() + 60)
    try:
        out = get_availability(7, for_date=None)
        assert out == {"by_day": {}, "slots_flat": []}
        m = gcs.get_availability_cache_metrics()
        assert m["cache_hit"] == 1
        assert m["cache_miss"] == 0
    finally:
        gcs._availability_cache.pop(k, None)


def test_availability_cache_metrics_miss_without_seed():
    """Cold request increments cache_miss (and usually calendar_unconfigured in dev/CI without credentials)."""
    gcs.reset_availability_cache_metrics()
    gcs._availability_cache.clear()
    get_availability(7, for_date=None)
    m = gcs.get_availability_cache_metrics()
    assert m["cache_miss"] >= 1
    assert m["cache_hit"] == 0


def test_availability_cache_profile_key_changes_with_slot_rules(monkeypatch):
    """Cache key profile should change when slot-shaping settings change."""
    monkeypatch.setattr(Config, "TIMEZONE", "America/Los_Angeles")
    monkeypatch.setattr(Config, "BUSINESS_APPOINTMENT_OPENING_TIME", "08:00")
    monkeypatch.setattr(Config, "BUSINESS_APPOINTMENT_CLOSING_TIME", "18:00")
    monkeypatch.setattr(Config, "BOOKING_DAYS_ENABLED", "mon,tue,wed")
    monkeypatch.setattr(Config, "BOOKING_SLOT_DURATION_MINUTES", 60)
    base = gcs._availability_cache_profile_key()
    monkeypatch.setattr(Config, "BOOKING_SLOT_DURATION_MINUTES", 30)
    changed = gcs._availability_cache_profile_key()
    assert base != changed


def test_get_availability_for_disabled_weekday_returns_closed_day(monkeypatch):
    """Specific-date availability should explain when weekday is disabled by BOOKING_DAYS_ENABLED."""
    monkeypatch.setenv("AVAILABILITY_CACHE_ENABLED", "false")
    monkeypatch.setattr(Config, "BOOKING_DAYS_ENABLED", "mon,tue,wed,thu,fri,sat")
    monkeypatch.setattr(gcs, "_get_calendar_service", lambda: (object(), "calendar_id"))
    gcs._availability_cache.clear()

    out = get_availability(days_ahead=7, for_date="2026-04-26")
    assert out.get("by_day") == {}
    assert out.get("slots_flat") == []
    closed = out.get("closed_day") or {}
    assert closed.get("date") == "2026-04-26"
    assert closed.get("weekday") == "sun"
    assert closed.get("weekday_display") == "Sunday"
    assert "sun" in (closed.get("closed_days") or [])
    assert "sat" in (closed.get("allowed_days") or [])


def test_edit_booking_past_slot():
    """edit_booking with a past start time rejects with clear message."""
    r = edit_booking("fake_event_id_does_not_exist", "2020-01-01T12:00:00Z", "1234567890")
    # Either "not found" (event doesn't exist) or "time has passed" (ownership check runs after get; if calendar not configured we get "not configured")
    assert isinstance(r, dict) and r.get("success") is False
    assert r.get("confirmed_slot") is None
    msg = (r.get("message") or "").lower()
    assert "passed" in msg or "not found" in msg or "not configured" in msg or "invalid" in msg or "specified" in msg


class _FakeEventsRequest:
    def __init__(self, response):
        self._response = response

    def execute(self):
        return self._response


class _FakeEventsApi:
    def __init__(self, items):
        self._items = items

    def list(self, **kwargs):
        return _FakeEventsRequest({"items": self._items})

    def list_next(self, request, response):
        return None


class _FakeCalendarService:
    def __init__(self, items=None, busy=None, cal_id="calendar_id"):
        self._events = _FakeEventsApi(items or [])
        self._freebusy = _FakeFreeBusyApi(busy or [], cal_id)

    def events(self):
        return self._events

    def freebusy(self):
        return self._freebusy


class _FakeFreeBusyRequest:
    def __init__(self, busy, cal_id):
        self._busy = busy
        self._cal_id = cal_id

    def execute(self):
        return {"calendars": {self._cal_id: {"busy": self._busy}}}


class _FakeFreeBusyApi:
    def __init__(self, busy, cal_id):
        self._busy = busy
        self._cal_id = cal_id
        self.last_body = None

    def query(self, body):
        self.last_body = body
        return _FakeFreeBusyRequest(self._busy, self._cal_id)


def test_get_availability_prefers_config_settings_and_orders_slots(monkeypatch):
    """Supabase-applied Config settings should shape availability, and slots_flat should be chronological."""
    monkeypatch.setenv("AVAILABILITY_CACHE_ENABLED", "false")
    monkeypatch.setenv("TIMEZONE", "UTC")
    monkeypatch.setenv("BUSINESS_APPOINTMENT_OPENING_TIME", "12:00")
    monkeypatch.setenv("BUSINESS_APPOINTMENT_CLOSING_TIME", "13:00")
    monkeypatch.setenv("BOOKING_SLOT_DURATION_MINUTES", "120")
    monkeypatch.setenv("AVAILABILITY_MAX_SLOTS_PER_BUCKET_PER_DAY", "1")
    monkeypatch.setenv("BOOKING_DAYS_ENABLED", "mon")

    monkeypatch.setattr(Config, "TIMEZONE", "America/Los_Angeles")
    monkeypatch.setattr(Config, "BUSINESS_APPOINTMENT_OPENING_TIME", "08:00")
    monkeypatch.setattr(Config, "BUSINESS_APPOINTMENT_CLOSING_TIME", "23:00")
    monkeypatch.setattr(Config, "BOOKING_SLOT_DURATION_MINUTES", 60)
    monkeypatch.setattr(Config, "AVAILABILITY_MAX_SLOTS_PER_BUCKET_PER_DAY", 0)
    monkeypatch.setattr(Config, "BOOKING_DAYS_ENABLED", "")
    monkeypatch.setattr(
        gcs,
        "_get_calendar_service",
        lambda: (_FakeCalendarService(busy=[], cal_id="calendar_id"), "calendar_id"),
    )
    gcs._availability_cache.clear()

    out = get_availability(days_ahead=7, for_date="2099-01-05")
    slots_flat = out.get("slots_flat") or []
    by_day = out.get("by_day") or {}
    day = by_day.get("2099-01-05") or {}

    starts = [slot["start"] for slot in slots_flat]
    assert starts == sorted(starts)
    assert len(slots_flat) == 15
    assert slots_flat[0]["display"].endswith("08:00 AM")
    assert slots_flat[-1]["display"].endswith("10:00 PM")
    assert len(day.get("morning") or []) == 4
    assert len(day.get("afternoon") or []) == 5
    assert len(day.get("evening") or []) == 6


def test_find_conflicting_event_detects_overlap():
    """Conflict check should find overlapping busy events."""
    service = _FakeCalendarService([
        {
            "id": "evt_1",
            "summary": "Existing booking",
            "status": "confirmed",
            "start": {"dateTime": "2026-04-16T17:00:00Z"},
            "end": {"dateTime": "2026-04-16T18:00:00Z"},
        }
    ])
    start_dt = gcs._parse_iso_datetime_utc("2026-04-16T17:30:00Z")
    end_dt = gcs._parse_iso_datetime_utc("2026-04-16T18:30:00Z")
    conflict = gcs._find_conflicting_event(
        service,
        "cal_id",
        start_dt,
        end_dt,
        "America/Los_Angeles",
    )
    assert isinstance(conflict, dict)
    assert conflict.get("event_id") == "evt_1"


def test_find_conflicting_event_ignores_event_id():
    """Reschedule check should ignore the caller's own event id when asked."""
    service = _FakeCalendarService([
        {
            "id": "evt_self",
            "summary": "Current booking",
            "status": "confirmed",
            "start": {"dateTime": "2026-04-16T17:00:00Z"},
            "end": {"dateTime": "2026-04-16T18:00:00Z"},
        }
    ])
    start_dt = gcs._parse_iso_datetime_utc("2026-04-16T17:00:00Z")
    end_dt = gcs._parse_iso_datetime_utc("2026-04-16T18:00:00Z")
    conflict = gcs._find_conflicting_event(
        service,
        "cal_id",
        start_dt,
        end_dt,
        "America/Los_Angeles",
        ignore_event_id="evt_self",
    )
    assert conflict is None


def run_list_my_bookings_test(contact_phone: str = "218-595-3061", contact_name: str | None = "Emil"):
    """Call list_my_bookings with given phone/name and print result (same as model call from logs)."""
    print(f"list_my_bookings(contact_phone={contact_phone!r}, contact_name={contact_name!r})")
    if not is_booking_enabled():
        print("  -> Booking not enabled; would return []")
        return
    out = list_my_bookings(contact_phone=contact_phone, contact_name=contact_name)
    print(f"  -> {len(out)} appointment(s)")
    for i, appt in enumerate(out, 1):
        print(f"     {i}. {appt.get('display', '')} (event_id={appt.get('event_id', '')})")
    if not out:
        print("  (No events with extendedProperties.private.caller_phone matching normalized phone)")
    return out


def run_edge_case_tests():
    """Run edge-case tests and raise on first failure."""
    test_phone_display_for_calendar_us()
    test_parse_booking_description_for_reschedule()
    test_build_booking_calendar_summary_and_description()
    test_normalize_phone_us_canonical()
    test_list_my_bookings_empty_phone()
    test_delete_booking_missing_event_id()
    test_edit_booking_missing_args()
    test_invalidate_availability_cache_clears_entries()
    test_invalidate_availability_cache_metrics_when_empty()
    test_availability_cache_metrics_hit_seeded()
    test_availability_cache_metrics_miss_without_seed()
    test_availability_cache_profile_key_changes_with_slot_rules()
    test_edit_booking_past_slot()
    test_find_conflicting_event_detects_overlap()
    test_find_conflicting_event_ignores_event_id()
    print("Edge-case tests passed.")


if __name__ == "__main__":
    main()
    print("\n--- list_my_bookings test (218-595-3061, Emil) ---")
    run_list_my_bookings_test("218-595-3061", "Emil")
    print("\nRunning edge-case tests...")
    run_edge_case_tests()
