"""
Google Calendar booking service: get availability and book appointments.
Uses Google Calendar via service account; config-driven so other providers can be added.
Returns availability grouped by day and by time-of-day (morning/afternoon/evening).
Optional in-memory cache for get_availability (pre-warmed when call starts).

Credentials: supports (1) file path via GOOGLE_CALENDAR_CREDENTIALS_JSON or
GOOGLE_APPLICATION_CREDENTIALS, or (2) inline JSON string (e.g. from Secret Manager
on GCP). If the env value looks like JSON (starts with '{'), it is used as
from_service_account_info(); otherwise treated as a path to a key file.

Edit/delete: Only events created with caller_phone in extendedProperties.private can be
listed (list_my_bookings), edited (edit_booking), or deleted (delete_booking). Events
created before this ownership field was added have no caller_phone; they will not
appear in list_my_bookings and edit/delete will return "You can only ... your own appointment."
"""
import json
import os
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from services.log_utils import Log

# Availability cache: key (days_ahead, for_date, booking_days_raw) -> (result_dict, expiry_ts). Thread-safe.
_availability_cache: dict[tuple[int, str, str], tuple[dict[str, Any], float]] = {}
_availability_cache_lock = threading.Lock()

# In-process counters for ops / debugging (thread-safe). Not reset on deploy except process restart.
_metrics_lock = threading.Lock()
_AVAILABILITY_CACHE_METRIC_KEYS = (
    "cache_hit",
    "cache_miss",
    "cache_store",
    "cache_expired_evicted",
    "invalidation_call",
    "invalidation_entries_cleared_sum",
    "get_availability_error",
    "get_availability_calendar_unconfigured",
)
_availability_cache_metrics: dict[str, int] = {k: 0 for k in _AVAILABILITY_CACHE_METRIC_KEYS}


def _metrics_inc(name: str, delta: int = 1) -> None:
    with _metrics_lock:
        _availability_cache_metrics[name] += delta


def get_availability_cache_metrics() -> dict[str, int]:
    """Return a copy of availability-cache counters (hits, misses, stores, invalidations, errors)."""
    with _metrics_lock:
        return dict(_availability_cache_metrics)


def reset_availability_cache_metrics() -> None:
    """Zero all availability-cache counters. Intended for tests; production uses process lifetime totals."""
    with _metrics_lock:
        for k in _AVAILABILITY_CACHE_METRIC_KEYS:
            _availability_cache_metrics[k] = 0


def invalidate_availability_cache(reason: str = "calendar_mutation") -> None:
    """
    Clear all cached get_availability results. Call after book_appointment, edit_booking, or delete_booking
    succeeds so the next get_availability reflects updated busy times (avoids stale free slots until TTL).
    """
    with _availability_cache_lock:
        n = len(_availability_cache)
        _availability_cache.clear()
    _metrics_inc("invalidation_call")
    _metrics_inc("invalidation_entries_cleared_sum", n)
    if n:
        Log.event(
            "Availability cache invalidated",
            {"reason": reason, "entries_cleared": n, "metrics": get_availability_cache_metrics()},
        )


def _availability_cache_ttl_seconds() -> int:
    return max(0, int(os.getenv("AVAILABILITY_CACHE_TTL_SECONDS", "90")))


def _availability_cache_enabled() -> bool:
    return os.getenv("AVAILABILITY_CACHE_ENABLED", "true").strip().lower() in ("true", "1", "yes")

from config import Config

# Optional Google Calendar
_google_calendar_id: str | None = None
_google_credentials_path: str | None = None
_google_credentials_info: dict[str, Any] | None = None


def _ensure_google_config() -> tuple[str | None, str | None, dict[str, Any] | None]:
    """Return (calendar_id, credentials_path, credentials_info). Path or info is set, not both."""
    global _google_calendar_id, _google_credentials_path, _google_credentials_info
    if _google_calendar_id is not None:
        return _google_calendar_id, _google_credentials_path, _google_credentials_info
    _google_calendar_id = os.getenv("GOOGLE_CALENDAR_ID", "").strip() or None
    raw = (
        os.getenv("GOOGLE_CALENDAR_CREDENTIALS_JSON") or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    )
    _google_credentials_path = None
    _google_credentials_info = None
    if raw:
        raw = raw.strip()
        # Inline JSON (e.g. Secret Manager value on GCP): use from_service_account_info
        if raw.startswith("{"):
            try:
                _google_credentials_info = json.loads(raw)
            except json.JSONDecodeError:
                Log.error("Google Calendar: GOOGLE_CALENDAR_CREDENTIALS_JSON looks like JSON but failed to parse; booking disabled.")
            # else: valid JSON, use _google_credentials_info
        elif os.path.isfile(raw):
            _google_credentials_path = raw
        else:
            Log.error("Google Calendar: credentials path not found or invalid (GOOGLE_CALENDAR_CREDENTIALS_JSON/GOOGLE_APPLICATION_CREDENTIALS); booking disabled.")
    elif _google_calendar_id:
        Log.error("Google Calendar: GOOGLE_CALENDAR_CREDENTIALS_JSON (and GOOGLE_APPLICATION_CREDENTIALS) not set; booking disabled.")
    return _google_calendar_id, _google_credentials_path, _google_credentials_info


def _get_calendar_service():
    """Build Google Calendar API service using service account. Returns None if not configured."""
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError:
        Log.info("Google Calendar libraries not installed; booking disabled.")
        return None, None
    cal_id, creds_path, creds_info = _ensure_google_config()
    if not cal_id:
        Log.error("Google Calendar: GOOGLE_CALENDAR_ID not set; booking disabled.")
        return None, cal_id
    has_creds = creds_path or creds_info
    if not has_creds:
        # _ensure_google_config already logged why creds are missing
        return None, cal_id
    try:
        SCOPES = ["https://www.googleapis.com/auth/calendar", "https://www.googleapis.com/auth/calendar.events"]
        if creds_info:
            creds = service_account.Credentials.from_service_account_info(creds_info, scopes=SCOPES)
        else:
            creds = service_account.Credentials.from_service_account_file(creds_path, scopes=SCOPES)
        service = build("calendar", "v3", credentials=creds)
        return service, cal_id
    except Exception as e:
        Log.error(f"Google Calendar init failed: {e}")
        return None, cal_id


def _effective_int_setting(name: str, default: int, minimum: int | None = None) -> int:
    """Read a runtime setting from Config first, then env; tolerate invalid dashboard/env values."""
    raw = getattr(Config, name, None)
    if raw is None or str(raw).strip() == "":
        raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    if minimum is not None:
        value = max(minimum, value)
    return value


def _effective_str_setting(name: str, default: str) -> str:
    """Read a runtime string setting from Config first, then env."""
    raw = getattr(Config, name, None)
    if raw is None or str(raw).strip() == "":
        raw = os.getenv(name, default)
    return str(raw or default).strip()


def _slot_duration_minutes() -> int:
    """Appointment length in minutes. Minimum 1 to avoid zero step and division by zero."""
    return _effective_int_setting("BOOKING_SLOT_DURATION_MINUTES", 60, minimum=1)


def _normalize_phone(phone: str) -> str:
    """
    Canonical form for ownership: digits only. Used for caller_phone on events and for list/edit/delete matching.
    US numbers: E.164 +12185953061 and formatted 218-595-3061 must match; we normalize 11-digit (1+10) to 10 digits
    so both become 2185953061 and list_my_bookings finds events created with either form.
    """
    if not phone or not isinstance(phone, str):
        return ""
    digits = "".join(c for c in phone if c.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        return digits[1:]  # US country code: canonicalize to 10 digits
    return digits


def _normalize_name(name: str) -> str:
    """Canonical form for name matching: strip, lowercase, collapse spaces. Empty string if missing."""
    if not name or not isinstance(name, str):
        return ""
    return " ".join((name or "").strip().lower().split())


def _phone_display_for_calendar(phone: str) -> str:
    """Human-friendly phone for event description (US 10-digit as (XXX) XXX-XXXX)."""
    d = _normalize_phone(phone)
    if len(d) == 10:
        return f"({d[:3]}) {d[3:6]}-{d[6:]}"
    if d:
        return d
    return (phone or "").strip() or "—"


def _company_name_for_calendar() -> str:
    try:
        n = getattr(Config, "COMPANY_NAME", None)
        if n and str(n).strip():
            return str(n).strip()
    except Exception:
        pass
    return (os.getenv("COMPANY_NAME") or "").strip() or "Our office"


def _truncate_calendar_field(text: str, max_len: int) -> str:
    text = (text or "").strip()
    if len(text) <= max_len:
        return text
    if max_len <= 3:
        return text[:max_len]
    return text[: max_len - 3].rstrip() + "..."


def _build_booking_calendar_summary(
    contact_name: str,
    visit_summary: str | None,
    company: str,
    category_display: str | None,
) -> str:
    """
    Event title shown in Google Calendar. Keeps length reasonable for mobile month view.
    """
    name = _truncate_calendar_field(contact_name, 72) or "Caller"
    company_t = _truncate_calendar_field(company, 48)
    reason = (visit_summary or "").strip()
    if reason:
        reason = _truncate_calendar_field(reason, 80)
        return f"{company_t} — {name}: {reason}"
    ind = (category_display or "").strip()
    if ind:
        ind = _truncate_calendar_field(ind, 40)
        return f"{company_t} — {name} · {ind}"
    return f"{company_t} — Phone booking · {name}"


def _parse_booking_description_for_reschedule(
    old_description: str | None,
) -> tuple[str | None, str | None, str | None]:
    """
    Extract email, visit notes, and service type from an event description written by
    book_appointment (or a prior reschedule). Returns (email, visit_notes, service_type).
    """
    if not old_description or not str(old_description).strip():
        return (None, None, None)
    text = str(old_description)
    email = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("Email:"):
            email = s.split("Email:", 1)[1].strip() or None

    visit_notes = None
    marker = "VISIT / NOTES"
    idx = text.find(marker)
    if idx != -1:
        after = text[idx + len(marker) :].lstrip("\n")
        note_lines: list[str] = []
        for line in after.splitlines():
            if line.startswith("  "):
                note_lines.append(line[2:].rstrip())
            elif line.strip() == "":
                if note_lines:
                    break
            else:
                break
        if note_lines:
            visit_notes = "\n".join(note_lines).strip() or None

    service_type = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("Service type:"):
            service_type = s.split("Service type:", 1)[1].strip() or None
            break

    return (email, visit_notes, service_type)


def _build_booking_calendar_description(
    *,
    contact_name: str,
    contact_phone: str,
    contact_email: str | None,
    visit_summary: str | None,
    when_local_display: str,
    when_local_long: str,
    tz_name: str,
    duration_minutes: int,
    company: str,
    category_display: str | None,
    rescheduled: bool = False,
) -> str:
    """Multi-line description for staff: structured sections, plain text (Calendar-friendly)."""
    header = (
        "Rescheduled via voice agent"
        if rescheduled
        else "Booked via voice agent"
    )
    lines = [
        header,
        "",
        "CALLER",
        f"  Name: {(contact_name or '').strip() or '—'}",
        f"  Phone: {_phone_display_for_calendar(contact_phone)}",
    ]
    if contact_email and str(contact_email).strip():
        lines.append(f"  Email: {str(contact_email).strip()}")
    lines.extend(
        [
            "",
            "APPOINTMENT",
            f"  Time: {when_local_long}",
            f"  Calendar time zone: {tz_name}",
            f"  Duration: {duration_minutes} minutes",
            f"  Quick ref: {when_local_display}",
        ]
    )
    if visit_summary and str(visit_summary).strip():
        lines.extend(["", "VISIT / NOTES", f"  {str(visit_summary).strip()}"])
    if category_display and str(category_display).strip():
        lines.extend(["", f"Service type: {str(category_display).strip()}"])
    lines.extend(
        [
            "",
            "—",
            f"{company} — Follow up using the phone above if needed.",
        ]
    )
    return "\n".join(lines)


def _timezone_str() -> str:
    return _effective_str_setting("TIMEZONE", "America/Los_Angeles")



def _business_hours() -> tuple[tuple[int, int], tuple[int, int]]:
    """
    Return ((open_hour, open_minute), (close_hour, close_minute)) in 24h from env.
    Default 08:00–18:00. Format: BUSINESS_APPOINTMENT_OPENING_TIME="08:00", BUSINESS_APPOINTMENT_CLOSING_TIME="18:00".
    """
    def parse_time(s: str, default_h: int, default_m: int) -> tuple[int, int]:
        s = (s or "").strip()
        if not s:
            return (default_h, default_m)
        parts = s.split(":")
        try:
            h = int(parts[0])
            m = int(parts[1]) if len(parts) > 1 else 0
            return (max(0, min(23, h)), max(0, min(59, m)))
        except (ValueError, IndexError):
            return (default_h, default_m)

    open_s = _effective_str_setting("BUSINESS_APPOINTMENT_OPENING_TIME", "08:00")
    close_s = _effective_str_setting("BUSINESS_APPOINTMENT_CLOSING_TIME", "18:00")
    return parse_time(open_s, 8, 0), parse_time(close_s, 18, 0)


def _slot_in_business_hours_utc(cursor_utc: datetime, tz_name: str) -> bool:
    """True if cursor_utc (UTC) falls within configured business hours in the given timezone."""
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        # Python < 3.9: fallback to UTC hour check (8–18 UTC)
        return 8 <= cursor_utc.hour < 18
    tz = ZoneInfo(tz_name)
    local_dt = cursor_utc.replace(tzinfo=timezone.utc).astimezone(tz)
    (open_h, open_m), (close_h, close_m) = _business_hours()
    local_minutes = local_dt.hour * 60 + local_dt.minute
    open_minutes = open_h * 60 + open_m
    close_minutes = close_h * 60 + close_m
    return open_minutes <= local_minutes < close_minutes


# Weekday names for booking-days filter (Python weekday: Mon=0 .. Sun=6)
_WEEKDAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_WEEKDAY_LABELS = {
    "mon": "Monday",
    "tue": "Tuesday",
    "wed": "Wednesday",
    "thu": "Thursday",
    "fri": "Friday",
    "sat": "Saturday",
    "sun": "Sunday",
}

_TIME_OF_DAY_BUCKET_ORDER = {"morning": 0, "afternoon": 1, "evening": 2}


def _booking_weekdays_raw() -> str:
    """Normalized BOOKING_DAYS_ENABLED string for cache key (env or override)."""
    raw = _effective_str_setting("BOOKING_DAYS_ENABLED", "")
    if not raw:
        return ""
    parts = [p.strip().lower() for p in raw.split(",") if p.strip()]
    valid = [p for p in parts if p in _WEEKDAY_NAMES]
    return ",".join(sorted(set(valid))) if valid else ""


def _availability_cache_profile_key() -> str:
    """
    Signature of settings that shape availability results.
    Keeps cache safe when booking config changes without deploy.
    """
    tz_name = _timezone_str().strip()
    (open_h, open_m), (close_h, close_m) = _business_hours()
    slot_minutes = _slot_duration_minutes()
    booking_days_raw = _booking_weekdays_raw()
    max_slots = _max_slots_per_bucket_per_day()
    return "|".join(
        [
            tz_name,
            f"{open_h:02d}:{open_m:02d}",
            f"{close_h:02d}:{close_m:02d}",
            str(slot_minutes),
            booking_days_raw,
            str(max_slots),
        ]
    )


def _booking_weekdays() -> frozenset[str] | None:
    """
    Set of weekday names that are enabled for booking (mon..sun).
    None or empty = all days bookable (backward compatible).
    """
    raw = _booking_weekdays_raw()
    if not raw:
        return None
    return frozenset(raw.split(","))


def _ordered_weekdays(day_set: frozenset[str] | None) -> list[str]:
    """Return weekday tokens in calendar order (mon..sun) for stable UI/tool messages."""
    if not day_set:
        return []
    return [day for day in _WEEKDAY_NAMES if day in day_set]


def _date_weekday_name(date_str: str) -> str:
    """Return weekday name (mon..sun) for YYYY-MM-DD."""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return _WEEKDAY_NAMES[dt.weekday()]
    except ValueError:
        return ""


# Time-of-day buckets (local hour): morning < 12, afternoon 12–16, evening 17 until close
def _time_of_day_bucket(local_hour: int) -> str:
    if local_hour < 12:
        return "morning"
    if local_hour < 17:
        return "afternoon"
    return "evening"


def _next_slot_boundary_local(now_local: datetime, slot_minutes: int) -> datetime:
    """Round up now_local to the next slot boundary (e.g. :00 and :30 for 30-min slots)."""
    slot_minutes = max(1, slot_minutes)  # avoid ZeroDivisionError
    total_minutes = now_local.hour * 60 + now_local.minute
    remainder = total_minutes % slot_minutes
    if remainder == 0 and now_local.second == 0 and now_local.microsecond == 0:
        next_boundary_minutes = total_minutes
    else:
        next_boundary_minutes = total_minutes + (slot_minutes - remainder)
    day_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    return day_start + timedelta(minutes=next_boundary_minutes)


def _to_rfc3339_utc(dt: datetime) -> str:
    """Return UTC RFC3339 string for Google API bounds."""
    return dt.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso_datetime_utc(value: str | None) -> datetime | None:
    """Parse ISO datetime and normalize to UTC (aware)."""
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _event_bounds_utc(item: dict[str, Any], tz_name: str) -> tuple[datetime | None, datetime | None]:
    """
    Return event (start_utc, end_utc) from Calendar event item.
    Supports dateTime and all-day date events.
    """
    start_obj = item.get("start") or {}
    end_obj = item.get("end") or {}
    if not isinstance(start_obj, dict) or not isinstance(end_obj, dict):
        return (None, None)

    start_dt_raw = start_obj.get("dateTime")
    end_dt_raw = end_obj.get("dateTime")
    if start_dt_raw and end_dt_raw:
        start_utc = _parse_iso_datetime_utc(start_dt_raw)
        end_utc = _parse_iso_datetime_utc(end_dt_raw)
        return (start_utc, end_utc)

    start_date = start_obj.get("date")
    end_date = end_obj.get("date")
    if start_date and end_date:
        try:
            from zoneinfo import ZoneInfo

            tz = ZoneInfo(tz_name)
        except Exception:
            tz = timezone.utc
        try:
            start_local = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=tz)
            end_local = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=tz)
            return (start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc))
        except ValueError:
            return (None, None)

    return (None, None)


def _find_conflicting_event(
    service,
    cal_id: str,
    start_dt_utc: datetime,
    end_dt_utc: datetime,
    tz_name: str,
    *,
    ignore_event_id: str | None = None,
) -> dict[str, Any] | None:
    """
    Find a conflicting busy event for [start_dt_utc, end_dt_utc).
    Returns a small event summary dict on conflict, else None.
    """
    request = service.events().list(
        calendarId=cal_id,
        timeMin=_to_rfc3339_utc(start_dt_utc),
        timeMax=_to_rfc3339_utc(end_dt_utc),
        singleEvents=True,
        orderBy="startTime",
        maxResults=50,
    )
    while request is not None:
        response = request.execute()
        for item in response.get("items", []):
            if not isinstance(item, dict):
                continue
            if (item.get("status") or "").lower() == "cancelled":
                continue
            if (item.get("transparency") or "").lower() == "transparent":
                continue
            event_id = (item.get("id") or "").strip()
            if ignore_event_id and event_id == ignore_event_id:
                continue
            event_start, event_end = _event_bounds_utc(item, tz_name)
            if not event_start or not event_end:
                continue
            if start_dt_utc < event_end and end_dt_utc > event_start:
                return {
                    "event_id": event_id or None,
                    "summary": (item.get("summary") or "").strip() or "Busy",
                    "start": _to_rfc3339_utc(event_start),
                    "end": _to_rfc3339_utc(event_end),
                }
        request = service.events().list_next(request, response)
    return None


def _max_slots_per_bucket_per_day() -> int:
    """
    Max slots per bucket (morning/afternoon/evening) per day.
    From env AVAILABILITY_MAX_SLOTS_PER_BUCKET_PER_DAY (default 4).
    Set to 0 to return all available slots (no cap) for the week.
    """
    v = _effective_int_setting("AVAILABILITY_MAX_SLOTS_PER_BUCKET_PER_DAY", 4)
    if v <= 0:
        return 9999  # no cap: include all slots in business hours
    return v


def get_availability(
    days_ahead: int = 7,
    slot_count: int | None = None,
    for_date: str | None = None,
) -> dict[str, Any]:
    """
    Return availability grouped by day and by time-of-day (morning/afternoon/evening).

    - If for_date is set (YYYY-MM-DD), returns slots only for that date.
    - Otherwise returns a full week (days_ahead) of slots so the AI has the schedule in hand.
    - Each day has up to AVAILABILITY_MAX_SLOTS_PER_BUCKET_PER_DAY slots per bucket (default 4); set to 0 for all slots.

    Returns:
        {
          "by_day": {
            "YYYY-MM-DD": {
              "date_display": "Today, Thu Feb 27" | "Tomorrow, Fri Feb 28" | "Wed, Mar 5",
              "morning": [{"start", "end", "display"}, ...],
              "afternoon": [...],
              "evening": [...]
            },
            ...
          },
          "slots_flat": [{"start", "end", "display"}, ...],  # all slots for book_appointment
          "closed_day": {  # optional; only when for_date is disabled by BOOKING_DAYS_ENABLED
            "date": "YYYY-MM-DD",
            "weekday": "sun",
            "weekday_display": "Sunday",
            "allowed_days": ["mon", ...],
            "closed_days": ["sun", ...]
          }
        }
    Empty result if Google Calendar is not configured or on error: {"by_day": {}, "slots_flat": []}.
    """
    empty = {"by_day": {}, "slots_flat": []}
    cache_profile = _availability_cache_profile_key()
    cache_key = (days_ahead, (for_date or "").strip(), cache_profile)
    cache_outcome: str | None = None
    cached_result: dict[str, Any] | None = None
    if _availability_cache_enabled():
        with _availability_cache_lock:
            entry = _availability_cache.get(cache_key)
            if entry is not None:
                result, expiry = entry
                if expiry > time.time():
                    cache_outcome = "hit"
                    cached_result = result
                else:
                    del _availability_cache[cache_key]
                    cache_outcome = "expired"
    if cache_outcome == "hit" and cached_result is not None:
        _metrics_inc("cache_hit")
        Log.event(
            "Availability cache hit",
            {"key": str(cache_key), "metrics": get_availability_cache_metrics()},
        )
        return cached_result
    if cache_outcome == "expired":
        _metrics_inc("cache_expired_evicted")
    if cache_outcome != "hit":
        _metrics_inc("cache_miss")

    service, cal_id = _get_calendar_service()
    if not service or not cal_id:
        _metrics_inc("get_availability_calendar_unconfigured")
        Log.error("get_availability: calendar not configured (check GOOGLE_CALENDAR_ID and GOOGLE_CALENDAR_CREDENTIALS_JSON); returning no slots.")
        return empty
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        ZoneInfo = None  # type: ignore

    try:
        tz_name = _timezone_str()
        tz = ZoneInfo(tz_name) if ZoneInfo else None
        now = datetime.now(timezone.utc)

        # Time window: either single day (for_date) or next days_ahead days
        if for_date:
            try:
                for_date_parsed = datetime.strptime(for_date.strip(), "%Y-%m-%d")
                allowed = _booking_weekdays()
                if allowed is not None:
                    day_name = _WEEKDAY_NAMES[for_date_parsed.weekday()]
                    if day_name not in allowed:
                        allowed_days = _ordered_weekdays(allowed)
                        closed_days = [day for day in _WEEKDAY_NAMES if day not in allowed]
                        return {
                            "by_day": {},
                            "slots_flat": [],
                            "closed_day": {
                                "date": for_date_parsed.strftime("%Y-%m-%d"),
                                "weekday": day_name,
                                "weekday_display": _WEEKDAY_LABELS.get(day_name, day_name.title()),
                                "allowed_days": allowed_days,
                                "closed_days": closed_days,
                            },
                        }
                local_open = for_date_parsed.replace(tzinfo=tz)
                local_close = local_open.replace(hour=23, minute=59, second=59, microsecond=0)
                (open_h, open_m), (close_h, close_m) = _business_hours()
                local_open = local_open.replace(hour=open_h, minute=open_m, second=0, microsecond=0)
                local_close = local_close.replace(hour=close_h, minute=close_m, second=0, microsecond=0)
                time_min = local_open.astimezone(timezone.utc).replace(microsecond=0)
                time_max = local_close.astimezone(timezone.utc).replace(microsecond=0)
                if time_max <= now:
                    return empty
                if time_min < now:
                    # Clamp to "now" then round up to next slot boundary in business timezone
                    slot_mins = _slot_duration_minutes()
                    if tz:
                        now_local = now.astimezone(tz)
                        next_local = _next_slot_boundary_local(now_local, slot_mins)
                        time_min = next_local.astimezone(timezone.utc).replace(microsecond=0)
                    else:
                        time_min = now.replace(microsecond=0)
                        total_m = time_min.hour * 60 + time_min.minute
                        rem = total_m % slot_mins
                        if rem == 0 and time_min.second == 0:
                            next_m = total_m
                        else:
                            next_m = total_m + (slot_mins - rem) if rem else total_m + slot_mins
                        day_start = time_min.replace(hour=0, minute=0, second=0, microsecond=0)
                        time_min = (day_start + timedelta(minutes=next_m)).replace(microsecond=0)
            except ValueError:
                return empty
        else:
            time_max = (now + timedelta(days=days_ahead)).replace(microsecond=0)
            # First candidate slot = next slot boundary in business timezone (e.g. :00 and :30 for 30-min slots)
            slot_mins = _slot_duration_minutes()
            if tz:
                now_local = now.astimezone(tz)
                next_local = _next_slot_boundary_local(now_local, slot_mins)
                time_min = next_local.astimezone(timezone.utc).replace(microsecond=0)
            else:
                time_min = now.replace(microsecond=0)
                slot_mins = max(1, slot_mins)
                total_m = time_min.hour * 60 + time_min.minute
                rem = total_m % slot_mins
                if rem == 0 and time_min.second == 0:
                    next_m = total_m
                else:
                    next_m = total_m + (slot_mins - rem) if rem else total_m + slot_mins
                day_start = time_min.replace(hour=0, minute=0, second=0, microsecond=0)
                time_min = (day_start + timedelta(minutes=next_m)).replace(microsecond=0)

        time_min_rfc = time_min.strftime("%Y-%m-%dT%H:%M:%SZ")
        time_max_rfc = time_max.strftime("%Y-%m-%dT%H:%M:%SZ")
        body = {"timeMin": time_min_rfc, "timeMax": time_max_rfc, "items": [{"id": cal_id}]}
        freebusy = service.freebusy().query(body=body).execute()
        busy_list = freebusy.get("calendars", {}).get(cal_id, {}).get("busy", [])

        def _parse_iso(s: str) -> datetime:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))

        busy_ranges = []
        for b in busy_list:
            start_s, end_s = b.get("start"), b.get("end")
            if start_s and end_s:
                busy_ranges.append((_parse_iso(start_s), _parse_iso(end_s)))

        duration = timedelta(minutes=_slot_duration_minutes())
        slots_raw: list[dict[str, Any]] = []
        cursor = time_min
        while cursor < time_max:
            end = cursor + duration
            overlap = any((cursor < be and end > bs) for bs, be in busy_ranges)
            if not overlap and _slot_in_business_hours_utc(cursor, tz_name):
                if tz:
                    local_dt = cursor.replace(tzinfo=timezone.utc).astimezone(tz)
                    display = local_dt.strftime("%a %b %d at %I:%M %p")
                    local_date_str = local_dt.strftime("%Y-%m-%d")
                    time_of_day = _time_of_day_bucket(local_dt.hour)
                else:
                    display = cursor.strftime("%a %b %d at %I:%M %p")
                    local_date_str = cursor.strftime("%Y-%m-%d")
                    time_of_day = "afternoon"
                allowed_days = _booking_weekdays()
                if allowed_days is not None and _date_weekday_name(local_date_str) not in allowed_days:
                    cursor += duration
                    continue
                slots_raw.append({
                    "start": cursor.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "display": display,
                    "local_date_str": local_date_str,
                    "time_of_day": time_of_day,
                })
            cursor += duration

        # Group by (date, bucket), cap per bucket per day (0 = no cap, return all 60-min slots)
        cap = _max_slots_per_bucket_per_day()
        by_date_bucket: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for s in slots_raw:
            key = (s["local_date_str"], s["time_of_day"])
            if len(by_date_bucket[key]) < cap:
                by_date_bucket[key].append(s)

        today_local = now.astimezone(tz) if tz else now
        today_str = today_local.strftime("%Y-%m-%d")
        tomorrow_dt = today_local + timedelta(days=1)
        tomorrow_str = tomorrow_dt.strftime("%Y-%m-%d")

        by_day: dict[str, dict[str, Any]] = {}
        slots_flat: list[dict[str, Any]] = []
        for (date_str, bucket), slot_list in sorted(
            by_date_bucket.items(),
            key=lambda x: (x[0][0], _TIME_OF_DAY_BUCKET_ORDER.get(x[0][1], 99)),
        ):
            if date_str not in by_day:
                if date_str == today_str:
                    date_display = f"Today, {today_local.strftime('%a %b %d')}"
                elif date_str == tomorrow_str:
                    date_display = f"Tomorrow, {tomorrow_dt.strftime('%a %b %d')}"
                else:
                    try:
                        d = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=tz)
                        date_display = d.strftime("%a, %b %d")
                    except ValueError:
                        date_display = date_str
                by_day[date_str] = {"date_display": date_display, "morning": [], "afternoon": [], "evening": []}
            for slot in slot_list:
                by_day[date_str][bucket].append({k: v for k, v in slot.items() if k in ("start", "end", "display")})
                slots_flat.append({k: v for k, v in slot.items() if k in ("start", "end", "display")})

        by_day_ordered = dict(sorted(by_day.items()))
        slots_flat.sort(key=lambda s: s.get("start", ""))
        result = {"by_day": by_day_ordered, "slots_flat": slots_flat}
        if _availability_cache_enabled():
            ttl = _availability_cache_ttl_seconds()
            if ttl > 0:
                with _availability_cache_lock:
                    _availability_cache[cache_key] = (result, time.time() + ttl)
                _metrics_inc("cache_store")
        return result
    except Exception as e:
        _metrics_inc("get_availability_error")
        Log.error(f"get_availability failed: {e}")
        return empty


def book_appointment(
    slot_start_iso: str,
    contact_name: str,
    contact_phone: str,
    contact_email: str | None = None,
    summary: str | None = None,
) -> dict[str, Any]:
    """
    Create a calendar event for the given slot and contact.
    Returns {"success": bool, "message": str, "event_id": str | None, "confirmed_slot": {...}, "calendar_event_link": str | None}.
    """
    service, cal_id = _get_calendar_service()
    if not service or not cal_id:
        return {
            "success": False,
            "message": "Booking is not configured.",
            "event_id": None,
            "confirmed_slot": None,
            "calendar_event_link": None,
        }
    try:
        start_dt = _parse_iso_datetime_utc(slot_start_iso)
        if not start_dt:
            return {
                "success": False,
                "message": "Invalid slot format. Please choose a slot from availability and try again.",
                "event_id": None,
                "confirmed_slot": None,
                "calendar_event_link": None,
            }
        end_dt = start_dt + timedelta(minutes=_slot_duration_minutes())
        if start_dt <= datetime.now(timezone.utc):
            return {
                "success": False,
                "message": "That time has already passed. Please pick another available slot.",
                "event_id": None,
                "confirmed_slot": None,
                "calendar_event_link": None,
            }
        tz_name = _timezone_str()
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(tz_name)
            local_dt = start_dt.astimezone(tz)
            display = local_dt.strftime("%a %b %d at %I:%M %p")
            message_time = local_dt.strftime("%A, %B %d at %I:%M %p")
        except Exception:
            display = start_dt.strftime("%a %b %d at %I:%M %p")
            message_time = start_dt.strftime("%A, %B %d at %I:%M %p")
        conflict = _find_conflicting_event(
            service,
            cal_id,
            start_dt,
            end_dt,
            tz_name,
        )
        if conflict:
            Log.event(
                "Booking conflict detected",
                {
                    "requested_start": _to_rfc3339_utc(start_dt),
                    "requested_end": _to_rfc3339_utc(end_dt),
                    "conflict_event_id": conflict.get("event_id"),
                    "conflict_summary": conflict.get("summary"),
                },
            )
            return {
                "success": False,
                "message": "That time was just taken. Please call get_availability again and offer another slot.",
                "event_id": None,
                "confirmed_slot": None,
                "calendar_event_link": None,
            }
        category_display = None
        company = _company_name_for_calendar()
        slot_mins = _slot_duration_minutes()
        event_summary = _build_booking_calendar_summary(
            contact_name, summary, company, category_display
        )
        event_description = _build_booking_calendar_description(
            contact_name=contact_name,
            contact_phone=contact_phone,
            contact_email=contact_email,
            visit_summary=summary,
            when_local_display=display,
            when_local_long=message_time,
            tz_name=tz_name,
            duration_minutes=slot_mins,
            company=company,
            category_display=category_display,
            rescheduled=False,
        )
        body = {
            "summary": event_summary,
            "description": event_description,
            "start": {"dateTime": start_dt.isoformat(), "timeZone": tz_name},
            "end": {"dateTime": end_dt.isoformat(), "timeZone": tz_name},
        }
        # Ownership: store caller phone and name so only they can list/edit/delete this booking
        normalized = _normalize_phone(contact_phone)
        name_stored = (contact_name or "").strip()
        if normalized or name_stored:
            body["extendedProperties"] = {"private": {}}
            if normalized:
                body["extendedProperties"]["private"]["caller_phone"] = normalized
            if name_stored:
                body["extendedProperties"]["private"]["caller_name"] = name_stored
        event = service.events().insert(calendarId=cal_id, body=body).execute()
        event_id = event.get("id")
        calendar_event_link = event.get("htmlLink")
        invalidate_availability_cache(reason="book_appointment")
        return {
            "success": True,
            "message": f"You're booked for {message_time}.",
            "event_id": event_id,
            "confirmed_slot": {
                "start": slot_start_iso,
                "end": end_dt.isoformat(),
                "display": display,
            },
            "calendar_event_link": calendar_event_link,
        }
    except Exception as e:
        Log.error(f"book_appointment failed: {e}")
        return {
            "success": False,
            "message": "We couldn't complete the booking. Our team will follow up to confirm.",
            "event_id": None,
            "confirmed_slot": None,
            "calendar_event_link": None,
        }


def list_my_bookings(contact_phone: str, contact_name: str | None = None) -> list[dict[str, Any]]:
    """
    Return current/future appointments for this caller (owned by contact_phone).
    Match is by normalized phone only so that name variants (e.g. Emil vs Emal, or different
    names on same number) do not hide valid bookings. contact_name is ignored for filtering.
    Used so the AI can say "You're booked on ..." and get event_id for edit_booking or delete_booking.
    Returns list entries with {"event_id", "start", "end", "display", "summary"} plus optional
    stored context such as {"caller_name", "visit_summary", "service_type"} to help the AI
    clarify which exact booking the caller means before mutation.
    """
    service, cal_id = _get_calendar_service()
    if not service or not cal_id:
        return []
    normalized = _normalize_phone(contact_phone)
    if not normalized:
        return []
    try:
        now = datetime.now(timezone.utc)
        time_min = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        tz_name = _timezone_str()
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(tz_name)
        except ImportError:
            tz = None
        request = service.events().list(
            calendarId=cal_id,
            timeMin=time_min,
            singleEvents=True,
            orderBy="startTime",
            maxResults=100,
        )
        out: list[dict[str, Any]] = []
        while request is not None:
            response = request.execute()
            for item in response.get("items", []):
                ep = item.get("extendedProperties") or {}
                priv = ep.get("private") if isinstance(ep, dict) else {}
                if not isinstance(priv, dict):
                    priv = {}
                if priv.get("caller_phone") != normalized:
                    continue
                event_id = item.get("id")
                if not event_id:
                    continue
                start_obj = item.get("start") or {}
                start_dt_str = start_obj.get("dateTime") or start_obj.get("date")
                if not start_dt_str:
                    continue
                end_obj = item.get("end") or {}
                end_dt_str = end_obj.get("dateTime") or end_obj.get("date") or ""
                summary = item.get("summary") or "Appointment"
                caller_name = str(priv.get("caller_name") or "").strip() or None
                _email_parsed, visit_summary, service_type = _parse_booking_description_for_reschedule(
                    item.get("description") if isinstance(item.get("description"), str) else None
                )
                try:
                    start_dt = datetime.fromisoformat((start_dt_str or "").replace("Z", "+00:00"))
                    if start_dt.tzinfo is None:
                        start_dt = start_dt.replace(tzinfo=timezone.utc)
                    start_dt_utc = start_dt.astimezone(timezone.utc)
                    start_iso = start_dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
                    if tz:
                        local_dt = start_dt_utc.astimezone(tz)
                        display = local_dt.strftime("%a %b %d at %I:%M %p")
                    else:
                        display = start_dt_utc.strftime("%a %b %d at %I:%M %p")
                    end_dt = datetime.fromisoformat((end_dt_str or "").replace("Z", "+00:00")) if end_dt_str else start_dt_utc + timedelta(minutes=_slot_duration_minutes())
                    if end_dt.tzinfo is None:
                        end_dt = end_dt.replace(tzinfo=timezone.utc)
                    end_iso = end_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                except (ValueError, TypeError):
                    start_iso = (start_dt_str or "").replace("Z", "+00:00")
                    end_iso = (end_dt_str or "").replace("Z", "+00:00")
                    display = start_iso
                out.append({
                    "event_id": event_id,
                    "start": start_iso,
                    "end": end_iso,
                    "display": display,
                    "summary": summary,
                    "caller_name": caller_name,
                    "visit_summary": visit_summary,
                    "service_type": service_type,
                })
            request = service.events().list_next(request, response)
        return sorted(out, key=lambda x: x.get("start", ""))
    except Exception as e:
        Log.error(f"list_my_bookings failed: {e}")
        return []


def delete_booking(event_id: str, contact_phone: str, contact_name: str | None = None) -> dict[str, Any]:
    """
    Cancel the caller's appointment. Ownership is by phone number only (contact_name is
    accepted for logging/display but not used as an ownership gate — AI transcription
    produces name variants like 'Nano'/'Nena' that must not block the rightful caller).
    Returns {"success": bool, "message": str}.
    """
    service, cal_id = _get_calendar_service()
    if not service or not cal_id:
        return {"success": False, "message": "Booking is not configured."}
    event_id = (event_id or "").strip()
    if not event_id:
        return {"success": False, "message": "Appointment not found."}
    normalized = _normalize_phone(contact_phone)
    try:
        event = service.events().get(calendarId=cal_id, eventId=event_id).execute()
    except Exception as e:
        Log.error(f"delete_booking get event failed: {e}")
        return {"success": False, "message": "Appointment not found."}
    ep = event.get("extendedProperties") or {}
    priv = ep.get("private") if isinstance(ep, dict) else {}
    if not isinstance(priv, dict):
        priv = {}
    if priv.get("caller_phone") != normalized:
        return {"success": False, "message": "You can only cancel your own appointment."}
    # Ownership is by phone only. Name is NOT checked here because AI speech-to-text
    # routinely produces variants (e.g. Nano/Nena/Nana) for the same caller, which
    # would block legitimate owners. The phone from Twilio caller-ID is deterministic.
    try:
        service.events().delete(calendarId=cal_id, eventId=event_id).execute()
        invalidate_availability_cache(reason="delete_booking")
        return {"success": True, "message": "Your appointment has been cancelled."}
    except Exception as e:
        Log.error(f"delete_booking delete failed: {e}")
        return {"success": False, "message": "We couldn't cancel the appointment. Our team will follow up."}


def edit_booking(
    event_id: str,
    new_slot_start_iso: str,
    contact_phone: str,
    contact_name: str | None = None,
) -> dict[str, Any]:
    """
    Reschedule the caller's appointment to a new slot. Ownership is by phone number only
    (contact_name is accepted for logging/display but not used as an ownership gate — AI
    transcription produces name variants like 'Nano'/'Nena' that must not block the rightful caller).
    Returns {"success": bool, "message": str, "confirmed_slot": { "start", "end", "display" } | None}.
    """
    service, cal_id = _get_calendar_service()
    if not service or not cal_id:
        return {"success": False, "message": "Booking is not configured.", "confirmed_slot": None}
    event_id = (event_id or "").strip()
    new_slot_start_iso = (new_slot_start_iso or "").strip()
    if not event_id or not new_slot_start_iso:
        return {"success": False, "message": "Appointment or new time not specified.", "confirmed_slot": None}
    normalized = _normalize_phone(contact_phone)
    start_dt = _parse_iso_datetime_utc(new_slot_start_iso)
    if not start_dt:
        return {"success": False, "message": "Invalid date or time. Use a slot from get_availability (ISO format).", "confirmed_slot": None}
    if start_dt <= datetime.now(timezone.utc):
        return {"success": False, "message": "That time has passed; please choose a future slot.", "confirmed_slot": None}
    try:
        event = service.events().get(calendarId=cal_id, eventId=event_id).execute()
    except Exception as e:
        Log.error(f"edit_booking get event failed: {e}")
        return {"success": False, "message": "Appointment not found.", "confirmed_slot": None}
    ep = event.get("extendedProperties") or {}
    priv = ep.get("private") if isinstance(ep, dict) else {}
    if not isinstance(priv, dict):
        priv = {}
    if priv.get("caller_phone") != normalized:
        return {"success": False, "message": "You can only reschedule your own appointment.", "confirmed_slot": None}
    # Ownership is by phone only. Name is NOT checked here because AI speech-to-text
    # routinely produces variants (e.g. Nano/Nena/Nana) for the same caller, which
    # would block legitimate owners. The phone from Twilio caller-ID is deterministic.
    tz_name = _timezone_str()
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_name)
        local_dt = start_dt.astimezone(tz)
        display = local_dt.strftime("%a %b %d at %I:%M %p")
        message_time = local_dt.strftime("%A, %B %d at %I:%M %p")
    except Exception:
        display = start_dt.strftime("%a %b %d at %I:%M %p")
        message_time = start_dt.strftime("%A, %B %d at %I:%M %p")
    end_dt = start_dt + timedelta(minutes=_slot_duration_minutes())
    conflict = _find_conflicting_event(
        service,
        cal_id,
        start_dt,
        end_dt,
        tz_name,
        ignore_event_id=event_id,
    )
    if conflict:
        Log.event(
            "Reschedule conflict detected",
            {
                "event_id": event_id,
                "requested_start": _to_rfc3339_utc(start_dt),
                "requested_end": _to_rfc3339_utc(end_dt),
                "conflict_event_id": conflict.get("event_id"),
                "conflict_summary": conflict.get("summary"),
            },
        )
        return {
            "success": False,
            "message": "That new time is no longer available. Please call get_availability again and choose another slot.",
            "confirmed_slot": None,
        }
    old_description = event.get("description")
    old_summary = event.get("summary")
    email_parsed, visit_notes, service_type_parsed = _parse_booking_description_for_reschedule(
        old_description if isinstance(old_description, str) else None
    )
    caller_name = (contact_name or "").strip() or (priv.get("caller_name") or "")
    company = _company_name_for_calendar()
    category_for_build = (service_type_parsed or "").strip() or None
    slot_mins = _slot_duration_minutes()
    if visit_notes:
        new_summary = _build_booking_calendar_summary(
            caller_name, visit_notes, company, category_for_build
        )
    elif old_summary and str(old_summary).strip():
        new_summary = str(old_summary).strip()
    else:
        new_summary = _build_booking_calendar_summary(
            caller_name, None, company, category_for_build
        )
    new_description = _build_booking_calendar_description(
        contact_name=caller_name,
        contact_phone=contact_phone,
        contact_email=email_parsed,
        visit_summary=visit_notes,
        when_local_display=display,
        when_local_long=message_time,
        tz_name=tz_name,
        duration_minutes=slot_mins,
        company=company,
        category_display=category_for_build,
        rescheduled=True,
    )
    body = {
        "summary": new_summary,
        "description": new_description,
        "start": {"dateTime": start_dt.isoformat(), "timeZone": tz_name},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": tz_name},
    }
    try:
        service.events().patch(calendarId=cal_id, eventId=event_id, body=body).execute()
    except Exception as e:
        Log.error(f"edit_booking patch failed: {e}")
        return {"success": False, "message": "We couldn't move the appointment. Our team will follow up.", "confirmed_slot": None}
    invalidate_availability_cache(reason="edit_booking")
    confirmed_slot = {
        "start": start_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end": end_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "display": display,
    }
    return {
        "success": True,
        "message": f"Your appointment has been moved to {message_time}.",
        "confirmed_slot": confirmed_slot,
    }


def is_booking_enabled() -> bool:
    """Return True if booking is enabled and Google Calendar is configured."""
    if not getattr(Config, "BOOKING_ENABLED", False):
        return False
    service, cal_id = _get_calendar_service()
    return bool(service and cal_id)

