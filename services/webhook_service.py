"""
Storage adapter for saved call records.

The public starter uses call-record language. This module still maps to the
storage-compatible Supabase schema so existing deployments can migrate
without an immediate database rewrite.
"""
import asyncio
import copy
from datetime import datetime, timezone
from typing import Any

import requests

from config import Config
from services.log_utils import Log


_UNSET = object()
BUSINESS_INTERACTION_HISTORY_LIMIT = 10


def _date_range_to_utc_iso(date_from_str: str | None, date_to_str: str | None) -> tuple[str | None, str | None]:
    """
    Convert date_from/date_to from Config.TIMEZONE (business TZ) to UTC ISO bounds for DB comparison.
    Accepts YYYY-MM-DD (all day: from=00:00, to=23:59:59) or YYYY-MM-DDTHH:mm or YYYY-MM-DDTHH:mm:ss.
    Returns (from_utc_iso, to_utc_iso). On invalid input or missing TIMEZONE, returns (None, None).
    """
    tz_name = (Config.TIMEZONE or "").strip()
    if not tz_name:
        return None, None
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_name)
    except Exception:
        return None, None
    from_iso: str | None = None
    to_iso: str | None = None
    def _parse_datetime(s: str) -> datetime | None:
        """Parse YYYY-MM-DD or YYYY-MM-DDTHH:mm or YYYY-MM-DDTHH:mm:ss in business TZ."""
        try:
            if "T" in s:
                try:
                    d = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
                except ValueError:
                    d = datetime.strptime(s, "%Y-%m-%dT%H:%M")
                return d.replace(tzinfo=tz)
            d = datetime.strptime(s, "%Y-%m-%d")
            return d.replace(tzinfo=tz, hour=0, minute=0, second=0, microsecond=0)
        except ValueError:
            return None

    if date_from_str and str(date_from_str).strip():
        s = str(date_from_str).strip()
        start_local = _parse_datetime(s)
        if start_local is not None:
            from_iso = start_local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if date_to_str and str(date_to_str).strip():
        s = str(date_to_str).strip()
        end_local = _parse_datetime(s)
        if end_local is not None:
            if "T" in s:
                end_local = end_local.replace(second=59, microsecond=999999)
            else:
                end_local = end_local.replace(hour=23, minute=59, second=59, microsecond=999999)
            to_iso = end_local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return from_iso, to_iso


def build_handoff_payload(
    *,
    contact: dict[str, Any],
    issue_summary: str,
    priority: str,
    call_summary: str,
    preferred_callback_time: str | None = None,
    confirmed_slot: dict[str, Any] | None = None,
    transcript: str | None = None,
    call_sid: str | None = None,
    service_address: str | None = None,
) -> dict[str, Any]:
    """Build the JSON payload for lead handoff (shared by all backends)."""
    return {
        "company_name": Config.COMPANY_NAME,
        "industry": Config.AGENT_LABEL,
        "agent_label": Config.AGENT_LABEL,
        "priority": priority,
        "contact": contact,
        "issue_summary": issue_summary,
        "call_summary": call_summary,
        "preferred_callback_time": preferred_callback_time,
        "confirmed_slot": confirmed_slot,
        "transcript": transcript,
        "call_sid": call_sid,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "service_address": service_address,
    }


def _normalize_phone_for_matching(value: Any) -> str:
    """Normalize phone-like values to comparable digits-only form (US +1 folded to 10 digits)."""
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        return digits[1:]
    return digits


def _coerce_metadata_dict(value: Any) -> dict[str, Any]:
    """Return a shallow-copy dict for metadata-like values; non-dicts become empty dicts."""
    if isinstance(value, dict):
        return copy.deepcopy(value)
    return {}


def _coerce_notes_list(value: Any) -> list[str]:
    """Return notes as a mutable list of strings."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def _coerce_appointments_list(value: Any) -> list[dict[str, Any]]:
    """Return appointment metadata as a mutable list of shallow dict copies."""
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict):
            out.append(copy.deepcopy(item))
    return out


def _append_unique_str(items: list[str], value: Any) -> list[str]:
    """Append a non-empty string to a list once, preserving order."""
    text = str(value or "").strip()
    if not text:
        return items
    if text not in items:
        items.append(text)
    return items


def _build_interaction_snapshot(lead: dict[str, Any]) -> dict[str, Any]:
    """Capture the current lead row's latest interaction before we repoint it to a new call."""
    return {
        "call_sid": lead.get("call_sid"),
        "timestamp": lead.get("timestamp") or lead.get("created_at"),
        "issue_summary": lead.get("issue_summary"),
        "call_summary": lead.get("call_summary"),
        "confirmed_slot": lead.get("confirmed_slot"),
        "calendar_event_link": lead.get("calendar_event_link"),
        "recording_link": lead.get("recording_link"),
        "transcript": lead.get("transcript"),
        "transcript_summary": lead.get("transcript_summary"),
        "transcript_issues": lead.get("transcript_issues"),
        "lead_status": lead.get("lead_status"),
        "service_address": lead.get("service_address"),
    }


def _appointment_sort_key(appointment: dict[str, Any]) -> tuple[int, str, str]:
    """Sort active appointments first, then by slot start / updated time / event id."""
    state = str(appointment.get("state") or "booked").strip().lower()
    slot = appointment.get("confirmed_slot") if isinstance(appointment.get("confirmed_slot"), dict) else {}
    start = str(slot.get("start") or slot.get("date_time") or slot.get("slot_start_iso") or "")
    updated_at = str(appointment.get("updated_at") or "")
    event_id = str(appointment.get("event_id") or "")
    return (0 if state == "booked" else 1, start or updated_at, event_id)


def _upsert_business_appointment(
    appointments_value: Any,
    *,
    event_id: str | None,
    state: str,
    confirmed_slot: Any = _UNSET,
    calendar_event_link: Any = _UNSET,
    summary: str | None = None,
    service_address: str | None = None,
    created_call_sid: str | None = None,
    last_interaction_call_sid: str | None = None,
    updated_at: str | None = None,
) -> list[dict[str, Any]]:
    """Upsert one appointment entry in metadata.appointments by calendar event id."""
    event_id_text = str(event_id or "").strip()
    appointments = _coerce_appointments_list(appointments_value)
    if not event_id_text:
        return sorted(appointments, key=_appointment_sort_key)

    appointment: dict[str, Any] | None = None
    for item in appointments:
        if str(item.get("event_id") or "").strip() == event_id_text:
            appointment = item
            break
    if appointment is None:
        appointment = {"event_id": event_id_text}
        appointments.append(appointment)

    state_text = str(state or "booked").strip().lower() or "booked"
    appointment["event_id"] = event_id_text
    appointment["state"] = state_text
    if confirmed_slot is not _UNSET:
        if confirmed_slot is not None:
            appointment["confirmed_slot"] = copy.deepcopy(confirmed_slot)
        elif state_text != "cancelled":
            appointment["confirmed_slot"] = None
    if calendar_event_link is not _UNSET:
        appointment["calendar_event_link"] = calendar_event_link
    summary_text = str(summary or "").strip()
    if summary_text:
        appointment["summary"] = summary_text
    service_address_text = str(service_address or "").strip()
    if service_address_text:
        appointment["service_address"] = service_address_text
    if created_call_sid:
        appointment.setdefault("created_call_sid", created_call_sid)
    if last_interaction_call_sid:
        appointment["last_interaction_call_sid"] = last_interaction_call_sid
    if updated_at:
        appointment["updated_at"] = updated_at
    return sorted(appointments, key=_appointment_sort_key)


def _derive_primary_booking_projection(appointments_value: Any) -> tuple[Any, Any]:
    """
    Derive the row-level confirmed_slot/calendar_event_link snapshot from metadata appointments.

    - 0 active appointments -> (None, None)
    - 1 active appointment -> mirror that exact slot/link
    - multiple active appointments -> summary slot, no single calendar link
    """
    appointments = _coerce_appointments_list(appointments_value)
    active = [
        item for item in appointments
        if str(item.get("state") or "booked").strip().lower() == "booked"
        and isinstance(item.get("confirmed_slot"), dict)
    ]
    if not active:
        return None, None
    active = sorted(active, key=_appointment_sort_key)
    if len(active) == 1:
        slot = copy.deepcopy(active[0].get("confirmed_slot"))
        return slot, active[0].get("calendar_event_link")
    first = active[0]
    first_slot = first.get("confirmed_slot") if isinstance(first.get("confirmed_slot"), dict) else {}
    first_display = str(first_slot.get("display") or first_slot.get("start") or "")
    summary_slot = {
        "display": f"{len(active)} active appointments",
        "multi_appointment": True,
        "active_count": len(active),
    }
    if first_display:
        summary_slot["primary_display"] = first_display
    return summary_slot, None


def _slot_display_text(slot: Any) -> str | None:
    """Return a human-friendly slot label when available."""
    if isinstance(slot, dict):
        for key in ("display", "date", "date_time", "slot_start_iso", "start"):
            text = str(slot.get(key) or "").strip()
            if text:
                return text
        return None
    text = str(slot or "").strip()
    return text or None


def _job_context_text(summary: str | None, service_address: str | None) -> str:
    """Build a durable business/job label for summaries."""
    summary_text = str(summary or "").strip()
    service_address_text = str(service_address or "").strip()
    if summary_text and service_address_text:
        return f"{summary_text} at {service_address_text}"
    if summary_text:
        return summary_text
    if service_address_text:
        return f"appointment at {service_address_text}"
    return "appointment"


def _build_last_booking_action_metadata(
    *,
    action: str,
    event_id: str | None,
    call_sid: str | None,
    timestamp: str,
    previous_confirmed_slot: Any = None,
    current_confirmed_slot: Any = None,
    summary: str | None = None,
    service_address: str | None = None,
) -> dict[str, Any]:
    """Build the latest booking-action metadata payload for the business row."""
    return {
        "type": str(action or "").strip().lower(),
        "event_id": str(event_id or "").strip() or None,
        "call_sid": str(call_sid or "").strip() or None,
        "timestamp": timestamp,
        "previous_confirmed_slot": copy.deepcopy(previous_confirmed_slot) if isinstance(previous_confirmed_slot, dict) else None,
        "current_confirmed_slot": copy.deepcopy(current_confirmed_slot) if isinstance(current_confirmed_slot, dict) else None,
        "summary": str(summary or "").strip() or None,
        "service_address": str(service_address or "").strip() or None,
    }


def _should_synthesize_booking_follow_up(metadata_value: Any, call_sid: str | None = None) -> bool:
    """True when this row represents a lifecycle follow-up booking-management interaction."""
    metadata = _coerce_metadata_dict(metadata_value)
    if str(metadata.get("business_record_mode") or "").strip().lower() != "lifecycle":
        return False
    last_booking_action = _coerce_metadata_dict(metadata.get("last_booking_action"))
    action_type = str(last_booking_action.get("type") or "").strip().lower()
    if action_type not in {"booked", "rescheduled", "cancelled"}:
        return False
    action_call_sid = str(last_booking_action.get("call_sid") or "").strip()
    if call_sid and action_call_sid and str(call_sid).strip() != action_call_sid:
        return False
    related_call_sids = metadata.get("related_call_sids")
    if not isinstance(related_call_sids, list):
        related_call_sids = []
    return len([str(item).strip() for item in related_call_sids if str(item).strip()]) >= 2


def _synthesize_booking_follow_up_summaries(
    lead: dict[str, Any],
    metadata_value: Any,
) -> tuple[str | None, str | None]:
    """Generate deterministic top-level summaries for lifecycle booking follow-up rows."""
    metadata = _coerce_metadata_dict(metadata_value)
    last_booking_action = _coerce_metadata_dict(metadata.get("last_booking_action"))
    action_type = str(last_booking_action.get("type") or "").strip().lower()
    if action_type not in {"booked", "rescheduled", "cancelled"}:
        return None, None

    event_id_text = str(last_booking_action.get("event_id") or metadata.get("booking_event_id") or "").strip()
    appointments = _coerce_appointments_list(metadata.get("appointments"))
    matching_appointment = None
    if event_id_text:
        for item in appointments:
            if str(item.get("event_id") or "").strip() == event_id_text:
                matching_appointment = item
                break
    if matching_appointment is None and len(appointments) == 1:
        matching_appointment = appointments[0]

    job_summary = (
        str(last_booking_action.get("summary") or "").strip()
        or str((matching_appointment or {}).get("summary") or "").strip()
        or str(lead.get("issue_summary") or "").strip()
        or None
    )
    service_address = (
        str(last_booking_action.get("service_address") or "").strip()
        or str((matching_appointment or {}).get("service_address") or "").strip()
        or str(lead.get("service_address") or "").strip()
        or None
    )
    job_context = _job_context_text(job_summary, service_address)
    caller_name = str(lead.get("lead_name") or "").strip() or "Caller"

    previous_slot = last_booking_action.get("previous_confirmed_slot")
    current_slot = last_booking_action.get("current_confirmed_slot")
    if not isinstance(previous_slot, dict):
        previous_slot = None
    if not isinstance(current_slot, dict):
        current_slot = None
    if current_slot is None and matching_appointment is not None and action_type != "cancelled":
        candidate = matching_appointment.get("confirmed_slot")
        current_slot = candidate if isinstance(candidate, dict) else None
    if previous_slot is None and matching_appointment is not None and action_type == "cancelled":
        candidate = matching_appointment.get("confirmed_slot")
        previous_slot = candidate if isinstance(candidate, dict) else None

    previous_display = _slot_display_text(previous_slot)
    current_display = _slot_display_text(current_slot)

    if action_type == "rescheduled":
        if previous_display and current_display and previous_display != current_display:
            return (
                f"{job_context}; appointment rescheduled from {previous_display} to {current_display}.",
                f"{caller_name} called to reschedule {job_context} from {previous_display} to {current_display}.",
            )
        if current_display:
            return (
                f"{job_context}; appointment rescheduled to {current_display}.",
                f"{caller_name} called to reschedule {job_context} to {current_display}.",
            )
        return (
            f"{job_context}; appointment rescheduled.",
            f"{caller_name} called to reschedule {job_context}.",
        )

    if action_type == "cancelled":
        if previous_display:
            return (
                f"{job_context}; appointment cancelled after being scheduled for {previous_display}.",
                f"{caller_name} called to cancel {job_context}, which had been scheduled for {previous_display}.",
            )
        return (
            f"{job_context}; appointment cancelled.",
            f"{caller_name} called to cancel {job_context}.",
        )

    if current_display:
        return (
            f"{job_context}; appointment booked for {current_display}.",
            f"{caller_name} booked {job_context} for {current_display}.",
        )
    return (
        f"{job_context}; appointment booked.",
        f"{caller_name} booked {job_context}.",
    )


def _merge_business_metadata(
    existing_metadata: Any,
    *,
    booking_event_id: str | None = None,
    booking_state: str | None = None,
    call_sid: str | None = None,
    interaction_type: str | None = None,
    archive_lead: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Merge lifecycle metadata without clobbering unrelated keys.

    Keys used by the business-record flow:
    - booking_event_id
    - booking_state
    - appointments
    - last_booking_action
    - related_call_sids
    - primary_call_sid
    - last_interaction_call_sid
    - last_interaction_type
    - interaction_history (latest snapshots from previous calls)
    """
    metadata = _coerce_metadata_dict(existing_metadata)
    related_call_sids = metadata.get("related_call_sids")
    if not isinstance(related_call_sids, list):
        related_call_sids = []
    related_call_sids = [str(item).strip() for item in related_call_sids if str(item).strip()]

    archived_call_sid = str((archive_lead or {}).get("call_sid") or "").strip()
    if archived_call_sid:
        _append_unique_str(related_call_sids, archived_call_sid)
    if call_sid:
        _append_unique_str(related_call_sids, call_sid)
    if related_call_sids:
        metadata["related_call_sids"] = related_call_sids
        metadata.setdefault("primary_call_sid", related_call_sids[0])

    if booking_event_id:
        metadata["booking_event_id"] = booking_event_id
    if booking_state:
        metadata["booking_state"] = booking_state
    if call_sid:
        metadata["last_interaction_call_sid"] = call_sid
    if interaction_type:
        metadata["last_interaction_type"] = interaction_type

    if archive_lead and archived_call_sid and archived_call_sid != str(call_sid or "").strip():
        history = metadata.get("interaction_history")
        if not isinstance(history, list):
            history = []
        snapshot = _build_interaction_snapshot(archive_lead)
        last_sid = ""
        if history:
            last = history[-1]
            if isinstance(last, dict):
                last_sid = str(last.get("call_sid") or "").strip()
        if snapshot.get("call_sid") and snapshot.get("call_sid") != last_sid:
            history.append(snapshot)
            history = history[-BUSINESS_INTERACTION_HISTORY_LIMIT:]
            metadata["interaction_history"] = history

    metadata["business_record_mode"] = "lifecycle"
    return metadata


# All supported backend names (CALL_RECORD_BACKEND env value, case-insensitive)
LEAD_BACKEND_NAMES = frozenset({
    "webhook", "supabase", "googlesheets", "email", "airtable", "sms", "telegram", "slack",
})


def has_lead_backend_configured() -> bool:
    """True if the selected lead backend is configured (so submit_lead tool is enabled)."""
    backend = (Config.LEAD_BACKEND or "webhook").strip().lower()
    if backend == "webhook":
        return bool(Config.WEBHOOK_URL and Config.WEBHOOK_URL.strip())
    if backend == "supabase":
        return bool(
            Config.SUPABASE_URL and Config.SUPABASE_URL.strip()
            and Config.SUPABASE_KEY and Config.SUPABASE_KEY.strip()
        )
    # googlesheets, email, airtable, sms, telegram, slack: add env checks when implemented
    return False


def _deliver_lead_webhook_sync(payload: dict[str, Any]) -> bool:
    """POST payload to WEBHOOK_URL. Returns True on success."""
    url = Config.WEBHOOK_URL
    if not url or not url.strip():
        Log.info("WEBHOOK_URL not set; skipping handoff POST")
        return False
    headers = {"Content-Type": "application/json"}
    if Config.WEBHOOK_SECRET and Config.WEBHOOK_SECRET.strip():
        headers["Authorization"] = f"Bearer {Config.WEBHOOK_SECRET.strip()}"
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=10)
        if r.ok:
            Log.info(f"Handoff webhook succeeded: {r.status_code}")
            return True
        Log.error(f"Handoff webhook failed: {r.status_code} {r.text[:200]}")
        return False
    except Exception as e:
        Log.error(f"Handoff webhook error: {e}")
        return False


def handoff_payload_to_supabase_row(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Build the Supabase row dict from a handoff payload.
    Used for both insert (first submit_lead) and update (second submit_lead by call_sid).
    """
    contact = payload.get("contact") or {}
    return {
        "company_name": payload.get("company_name"),
        "industry": payload.get("industry"),
        "priority": payload.get("priority"),
        "lead_name": contact.get("name") or None,
        "lead_email": contact.get("email") or None,
        "lead_phone": contact.get("phone") or None,
        "issue_summary": payload.get("issue_summary"),
        "call_summary": payload.get("call_summary"),
        "preferred_callback_time": payload.get("preferred_callback_time"),
        "confirmed_slot": payload.get("confirmed_slot"),
        "transcript": payload.get("transcript"),
        "call_sid": payload.get("call_sid"),
        "timestamp": payload.get("timestamp"),
        "calendar_event_link": payload.get("calendar_event_link"),
        "recording_link": payload.get("recording_link"),
        "metadata": payload.get("metadata"),
        "service_address": payload.get("service_address"),
    }


def handoff_payload_to_supabase_updates(
    payload: dict[str, Any],
    *,
    include_nulls: bool = False,
) -> dict[str, Any]:
    """Build a safe Supabase update dict from a handoff payload."""
    row = handoff_payload_to_supabase_row(payload)
    if include_nulls:
        return row
    return {k: v for k, v in row.items() if v is not None}


def _deliver_lead_supabase_sync(payload: dict[str, Any]) -> bool:
    """Insert lead into Supabase table. Returns True on success."""
    try:
        from supabase import create_client
    except ImportError:
        Log.error("supabase package not installed; pip install supabase")
        return False
    url = Config.SUPABASE_URL
    key = Config.SUPABASE_KEY
    table = Config.SUPABASE_LEAD_TABLE or "leads"
    if not url or not key:
        Log.info("SUPABASE_URL/SUPABASE_KEY not set; skipping Supabase insert")
        return False
    try:
        client = create_client(url.strip(), key.strip())
        row = handoff_payload_to_supabase_row(payload)
        client.table(table).insert(row).execute()
        Log.info(f"Lead inserted into Supabase table {table}")
        return True
    except Exception as e:
        Log.error(f"Supabase lead insert error: {e}")
        return False


def update_lead_by_call_sid_sync(call_sid: str | None, updates: dict[str, Any]) -> bool:
    """
    Update the lead row for the given call_sid with the given column values.
    Only runs when CALL_RECORD_BACKEND=supabase and Supabase is configured. Returns True on success.
    """
    backend = (Config.LEAD_BACKEND or "webhook").strip().lower()
    if backend != "supabase":
        return False
    call_sid = (str(call_sid).strip() if call_sid else "")
    if not call_sid:
        return False
    updates = {k: v for k, v in (updates or {}).items() if k}
    if not updates:
        return False
    if not Config.SUPABASE_URL or not Config.SUPABASE_KEY:
        return False
    try:
        from supabase import create_client
    except ImportError:
        Log.error("supabase package not installed; pip install supabase")
        return False
    table = Config.SUPABASE_LEAD_TABLE or "leads"
    try:
        client = create_client(Config.SUPABASE_URL.strip(), Config.SUPABASE_KEY.strip())
        client.table(table).update(updates).eq("call_sid", call_sid).execute()
        Log.info(f"Lead updated by call_sid for table {table}")
        return True
    except Exception as e:
        Log.error(f"Supabase lead update by call_sid error: {e}")
        return False


async def update_lead_by_call_sid_async(call_sid: str | None, updates: dict[str, Any]) -> bool:
    """Update lead by call_sid in a thread so we don't block the event loop."""
    loop = asyncio.get_event_loop()
    ok = await loop.run_in_executor(None, lambda: update_lead_by_call_sid_sync(call_sid, updates))
    if ok and (Config.LEAD_BACKEND or "webhook").strip().lower() == "supabase":
        from services.call_record_events import notify_call_records_changed_async

        await notify_call_records_changed_async()
    return ok


def get_lead_by_call_sid_sync(call_sid: str | None) -> dict[str, Any] | None:
    """
    Fetch the lead row for the given call_sid. Returns the row as a dict or None if not found.
    Only runs when CALL_RECORD_BACKEND=supabase and Supabase is configured.
    """
    backend = (Config.LEAD_BACKEND or "webhook").strip().lower()
    if backend != "supabase":
        return None
    call_sid = (str(call_sid).strip() if call_sid else "")
    if not call_sid or not Config.SUPABASE_URL or not Config.SUPABASE_KEY:
        return None
    try:
        from supabase import create_client
    except ImportError:
        return None
    table = Config.SUPABASE_LEAD_TABLE or "leads"
    try:
        client = create_client(Config.SUPABASE_URL.strip(), Config.SUPABASE_KEY.strip())
        r = client.table(table).select("*").eq("call_sid", call_sid).limit(1).execute()
        data = (r.data or []) if hasattr(r, "data") else []
        return data[0] if data else None
    except Exception as e:
        Log.error(f"Supabase get lead by call_sid error: {e}")
        return None


def append_lead_note_by_call_sid_sync(call_sid: str | None, note: str) -> bool:
    """
    Append a single note to the lead's notes (jsonb array) for the given call_sid.
    Used to record booking actions (cancelled, edited) without overwriting call_summary.
    Only runs when CALL_RECORD_BACKEND=supabase and a row exists for call_sid. Returns True on success.
    """
    if not call_sid or not (str(note).strip()):
        return False
    lead = get_lead_by_call_sid_sync(call_sid)
    if not lead:
        return False
    notes = lead.get("notes")
    if notes is None:
        notes = []
    if not isinstance(notes, list):
        notes = [str(notes)] if notes else []
    notes = list(notes)
    notes.append(str(note).strip())
    return update_lead_by_call_sid_sync(call_sid, {"notes": notes})


async def append_lead_note_by_call_sid_async(call_sid: str | None, note: str) -> bool:
    """Append a note to the lead by call_sid in a thread so we don't block the event loop."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: append_lead_note_by_call_sid_sync(call_sid, note))


# Allowed keys for dashboard updates (notes, lead_status, lead_name, service_address, transcript, transcript_enhanced_at, transcript_summary, transcript_issues)
LEAD_UPDATE_ALLOWED_KEYS = frozenset({
    "notes", "lead_status", "lead_name", "service_address", "transcript", "transcript_enhanced_at",
    "transcript_summary", "transcript_issues",
})
SYSTEM_LEAD_UPDATE_ALLOWED_KEYS = LEAD_UPDATE_ALLOWED_KEYS | frozenset({
    "company_name",
    "industry",
    "priority",
    "lead_email",
    "lead_phone",
    "issue_summary",
    "call_summary",
    "preferred_callback_time",
    "confirmed_slot",
    "call_sid",
    "timestamp",
    "calendar_event_link",
    "recording_link",
    "metadata",
})


class LeadUpdateSchemaError(Exception):
    """Raised when lead update fails because table is missing notes/lead_status columns."""
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def get_lead_by_id_sync(lead_id: str | None) -> dict[str, Any] | None:
    """
    Fetch a single lead by primary key id. Returns the lead row as a dict or None if not found.
    Only runs when CALL_RECORD_BACKEND=supabase.
    """
    backend = (Config.LEAD_BACKEND or "webhook").strip().lower()
    if backend != "supabase":
        return None
    lead_id = (str(lead_id).strip() if lead_id else "")
    if not lead_id:
        return None
    if not Config.SUPABASE_URL or not Config.SUPABASE_KEY:
        return None
    try:
        from supabase import create_client
    except ImportError:
        Log.error("supabase package not installed; pip install supabase")
        return None
    table = Config.SUPABASE_LEAD_TABLE or "leads"
    try:
        client = create_client(Config.SUPABASE_URL.strip(), Config.SUPABASE_KEY.strip())
        r = client.table(table).select("*").eq("id", lead_id).limit(1).execute()
        data = (r.data or []) if hasattr(r, "data") else []
        return data[0] if data else None
    except Exception as e:
        Log.error(f"Supabase get lead by id error: {e}")
        return None


def delete_lead_by_id_sync(lead_id: str | None) -> bool:
    """
    Permanently delete a lead row by primary key id.
    Only runs when CALL_RECORD_BACKEND=supabase. Returns True on success.
    """
    backend = (Config.LEAD_BACKEND or "webhook").strip().lower()
    if backend != "supabase":
        return False
    lead_id = (str(lead_id).strip() if lead_id else "")
    if not lead_id:
        return False
    if not Config.SUPABASE_URL or not Config.SUPABASE_KEY:
        return False
    try:
        from supabase import create_client
    except ImportError:
        Log.error("supabase package not installed; pip install supabase")
        return False
    table = Config.SUPABASE_LEAD_TABLE or "leads"
    try:
        client = create_client(Config.SUPABASE_URL.strip(), Config.SUPABASE_KEY.strip())
        r = client.table(table).delete().eq("id", lead_id).execute()
        deleted = (r.data or []) if hasattr(r, "data") else []
        if not deleted:
            return False
        Log.info(f"Lead deleted by id for table {table}")
        return True
    except Exception as e:
        Log.error(f"Supabase lead delete by id error: {e}")
        return False


def _update_lead_by_id_sync(
    lead_id: str | None,
    updates: dict[str, Any],
    *,
    allowed_keys: frozenset[str],
) -> bool:
    """
    Update the lead row by primary key id (e.g. UUID from Supabase).
    Only runs when CALL_RECORD_BACKEND=supabase.
    Returns True on success. Raises LeadUpdateSchemaError when columns are missing.
    """
    backend = (Config.LEAD_BACKEND or "webhook").strip().lower()
    if backend != "supabase":
        return False
    lead_id = (str(lead_id).strip() if lead_id else "")
    if not lead_id:
        return False
    updates = {k: v for k, v in (updates or {}).items() if k in allowed_keys}
    if not updates:
        return False
    if not Config.SUPABASE_URL or not Config.SUPABASE_KEY:
        return False
    try:
        from supabase import create_client
    except ImportError:
        Log.error("supabase package not installed; pip install supabase")
        return False
    table = Config.SUPABASE_LEAD_TABLE or "leads"
    try:
        client = create_client(Config.SUPABASE_URL.strip(), Config.SUPABASE_KEY.strip())
        client.table(table).update(updates).eq("id", lead_id).execute()
        Log.info(f"Lead updated by id for table {table}")
        return True
    except Exception as e:
        err_str = str(e)
        err_lower = err_str.lower()
        # PostgREST schema cache / column not found (e.g. notes or lead_status missing)
        if "PGRST204" in err_str or "schema cache" in err_lower or ("column" in err_lower and "does not exist" in err_lower):
            raise LeadUpdateSchemaError(
                "The call-record table is missing 'notes' or 'lead_status' columns. "
                "Run docs/supabase-schema/call_records_schema.sql in the Supabase SQL Editor."
            ) from e
        Log.error(f"Supabase lead update by id error: {e}")
        return False


def update_lead_by_id_sync(lead_id: str | None, updates: dict[str, Any]) -> bool:
    """
    Dashboard-safe lead updates by id.
    Allows notes, lead_status, lead_name, service_address, transcript, transcript_enhanced_at,
    transcript_summary, and transcript_issues.
    """
    return _update_lead_by_id_sync(lead_id, updates, allowed_keys=LEAD_UPDATE_ALLOWED_KEYS)


def update_lead_system_fields_by_id_sync(lead_id: str | None, updates: dict[str, Any]) -> bool:
    """Internal lead updates by id for business-record lifecycle fields."""
    return _update_lead_by_id_sync(lead_id, updates, allowed_keys=SYSTEM_LEAD_UPDATE_ALLOWED_KEYS)


async def update_lead_system_fields_by_id_async(lead_id: str | None, updates: dict[str, Any]) -> bool:
    """Async wrapper for internal id-based lead updates."""
    loop = asyncio.get_event_loop()
    ok = await loop.run_in_executor(None, lambda: update_lead_system_fields_by_id_sync(lead_id, updates))
    if ok and (Config.LEAD_BACKEND or "webhook").strip().lower() == "supabase":
        from services.call_record_events import notify_call_records_changed_async

        await notify_call_records_changed_async()
    return ok


def find_related_lead_for_booking_sync(
    event_id: str | None,
    contact_phone: str | None = None,
    *,
    limit: int = 200,
) -> dict[str, Any] | None:
    """
    Find the business-record lead row that owns a booking.

    Matching strategy:
    1. Exact metadata.booking_event_id match (preferred).
    2. Exact metadata.appointments[].event_id match.
    3. calendar_event_link string containing event_id (fallback for older rows).
    4. If there is exactly one active-booking row for the normalized phone, use it.
    """
    backend = (Config.LEAD_BACKEND or "webhook").strip().lower()
    if backend != "supabase":
        return None
    if not Config.SUPABASE_URL or not Config.SUPABASE_KEY:
        return None
    try:
        from supabase import create_client
    except ImportError:
        Log.error("supabase package not installed; pip install supabase")
        return None

    table = Config.SUPABASE_LEAD_TABLE or "leads"
    try:
        client = create_client(Config.SUPABASE_URL.strip(), Config.SUPABASE_KEY.strip())
        response = client.table(table).select("*").limit(limit).execute()
        rows = (response.data or []) if hasattr(response, "data") else []
    except Exception as e:
        Log.error(f"Supabase find related lead for booking error: {e}")
        return None

    def _sort_key(row: dict[str, Any]) -> str:
        return str(row.get("created_at") or row.get("timestamp") or "")

    rows = sorted(rows, key=_sort_key, reverse=True)
    event_id_text = str(event_id or "").strip()
    if event_id_text:
        exact_matches = []
        for row in rows:
            metadata = _coerce_metadata_dict(row.get("metadata"))
            booking_event_id = str(metadata.get("booking_event_id") or "").strip()
            if booking_event_id and booking_event_id == event_id_text:
                exact_matches.append(row)
                continue
            appointments = _coerce_appointments_list(metadata.get("appointments"))
            if any(str(item.get("event_id") or "").strip() == event_id_text for item in appointments):
                exact_matches.append(row)
                continue
            calendar_link = str(row.get("calendar_event_link") or "").strip()
            if calendar_link and event_id_text in calendar_link:
                exact_matches.append(row)
        if exact_matches:
            return exact_matches[0]

    normalized_phone = _normalize_phone_for_matching(contact_phone)
    if not normalized_phone:
        return None
    phone_matches = [
        row for row in rows
        if _normalize_phone_for_matching(row.get("lead_phone")) == normalized_phone
        and row.get("confirmed_slot")
    ]
    if len(phone_matches) == 1:
        return phone_matches[0]
    return None


def sync_business_lead_after_booking_action_sync(
    *,
    action: str,
    call_sid: str | None,
    event_id: str | None = None,
    contact_phone: str | None = None,
    confirmed_slot: Any = _UNSET,
    calendar_event_link: Any = _UNSET,
    note: str | None = None,
    appointment_summary: str | None = None,
    service_address: str | None = None,
) -> str | None:
    """
    Sync the primary business-record lead row after booking/cancel/reschedule.

    Returns the resolved lead id when a row was updated, else None.
    """
    backend = (Config.LEAD_BACKEND or "webhook").strip().lower()
    if backend != "supabase":
        return None

    action_text = (action or "").strip().lower()
    if action_text not in {"booked", "cancelled", "rescheduled"}:
        return None

    lead = None
    if action_text == "booked" and call_sid:
        lead = get_lead_by_call_sid_sync(call_sid)
    else:
        lead = find_related_lead_for_booking_sync(event_id=event_id, contact_phone=contact_phone)
        if not lead and call_sid:
            lead = get_lead_by_call_sid_sync(call_sid)
    if not lead or not lead.get("id"):
        return None

    current_call_sid = str(call_sid or "").strip() or None
    existing_call_sid = str(lead.get("call_sid") or "").strip() or None
    archive_lead = lead if existing_call_sid and current_call_sid and existing_call_sid != current_call_sid else None
    booking_state = "cancelled" if action_text == "cancelled" else "booked"
    now_iso = datetime.now(timezone.utc).isoformat()
    existing_metadata = _coerce_metadata_dict(lead.get("metadata"))
    appointments = _coerce_appointments_list(existing_metadata.get("appointments"))
    event_id_text = str(event_id or "").strip() or None
    existing_appointment = None
    if event_id_text:
        for item in appointments:
            if str(item.get("event_id") or "").strip() == event_id_text:
                existing_appointment = item
                break
    resolved_summary = (
        str(appointment_summary or "").strip()
        or str((existing_appointment or {}).get("summary") or "").strip()
        or str(lead.get("issue_summary") or "").strip()
        or None
    )
    resolved_service_address = (
        str(service_address or "").strip()
        or str((existing_appointment or {}).get("service_address") or "").strip()
        or str(lead.get("service_address") or "").strip()
        or None
    )
    appointments = _upsert_business_appointment(
        appointments,
        event_id=event_id_text,
        state=booking_state,
        confirmed_slot=confirmed_slot,
        calendar_event_link=calendar_event_link,
        summary=resolved_summary,
        service_address=resolved_service_address,
        created_call_sid=(current_call_sid or existing_call_sid),
        last_interaction_call_sid=current_call_sid,
        updated_at=now_iso,
    )
    previous_confirmed_slot = (
        copy.deepcopy(existing_appointment.get("confirmed_slot"))
        if isinstance((existing_appointment or {}).get("confirmed_slot"), dict)
        else None
    )
    current_confirmed_slot = (
        copy.deepcopy(confirmed_slot)
        if confirmed_slot is not _UNSET and isinstance(confirmed_slot, dict)
        else None
    )
    metadata = _merge_business_metadata(
        existing_metadata,
        booking_event_id=event_id_text,
        booking_state=booking_state,
        call_sid=current_call_sid,
        interaction_type=action_text,
        archive_lead=archive_lead,
    )
    if appointments:
        metadata["appointments"] = appointments
    metadata["last_booking_action"] = _build_last_booking_action_metadata(
        action=action_text,
        event_id=event_id_text,
        call_sid=current_call_sid,
        timestamp=now_iso,
        previous_confirmed_slot=previous_confirmed_slot,
        current_confirmed_slot=current_confirmed_slot,
        summary=resolved_summary,
        service_address=resolved_service_address,
    )

    updates: dict[str, Any] = {
        "metadata": metadata,
        "timestamp": now_iso,
    }
    if current_call_sid:
        updates["call_sid"] = current_call_sid
    projected_slot, projected_link = _derive_primary_booking_projection(appointments)
    if appointments:
        updates["confirmed_slot"] = projected_slot
        updates["calendar_event_link"] = projected_link
        has_active_booking = projected_slot is not None
        has_cancelled_booking = any(
            str(item.get("state") or "").strip().lower() == "cancelled" for item in appointments
        )
        if has_active_booking:
            updates["lead_status"] = "booked"
        elif has_cancelled_booking:
            updates["lead_status"] = "cancelled"
    elif action_text == "cancelled":
        updates["confirmed_slot"] = None if confirmed_slot is _UNSET else confirmed_slot
        updates["calendar_event_link"] = None if calendar_event_link is _UNSET else calendar_event_link
        updates["lead_status"] = "cancelled"
    else:
        if confirmed_slot is not _UNSET:
            updates["confirmed_slot"] = confirmed_slot
        if calendar_event_link is not _UNSET:
            updates["calendar_event_link"] = calendar_event_link
        updates["lead_status"] = "booked"

    note_text = str(note or "").strip()
    if note_text:
        notes = _coerce_notes_list(lead.get("notes"))
        notes.append(note_text)
        updates["notes"] = notes
    if archive_lead is not None:
        summary_lead = dict(lead)
        summary_lead["service_address"] = resolved_service_address
        issue_summary, call_summary = _synthesize_booking_follow_up_summaries(summary_lead, metadata)
        if issue_summary:
            updates["issue_summary"] = issue_summary
        if call_summary:
            updates["call_summary"] = call_summary

    ok = update_lead_system_fields_by_id_sync(lead.get("id"), updates)
    return str(lead.get("id")).strip() if ok else None


async def sync_business_lead_after_booking_action_async(
    *,
    action: str,
    call_sid: str | None,
    event_id: str | None = None,
    contact_phone: str | None = None,
    confirmed_slot: Any = _UNSET,
    calendar_event_link: Any = _UNSET,
    note: str | None = None,
    appointment_summary: str | None = None,
    service_address: str | None = None,
) -> str | None:
    """Async wrapper for booking lifecycle business-record sync."""
    loop = asyncio.get_event_loop()
    lead_id = await loop.run_in_executor(
        None,
        lambda: sync_business_lead_after_booking_action_sync(
            action=action,
            call_sid=call_sid,
            event_id=event_id,
            contact_phone=contact_phone,
            confirmed_slot=confirmed_slot,
            calendar_event_link=calendar_event_link,
            note=note,
            appointment_summary=appointment_summary,
            service_address=service_address,
        ),
    )
    if lead_id and (Config.LEAD_BACKEND or "webhook").strip().lower() == "supabase":
        from services.call_record_events import notify_call_records_changed_async

        await notify_call_records_changed_async()
    return lead_id


def update_business_lead_from_payload_sync(lead_id: str | None, payload: dict[str, Any]) -> bool:
    """
    Update an already-resolved business-record lead row from a submit_lead payload.
    Preserves existing booking fields and lifecycle metadata unless explicitly provided.
    """
    lead = get_lead_by_id_sync(lead_id)
    if not lead:
        return False
    updates = handoff_payload_to_supabase_updates(payload, include_nulls=False)
    metadata = _merge_business_metadata(
        lead.get("metadata"),
        call_sid=str(payload.get("call_sid") or "").strip() or None,
        interaction_type="submit_lead_follow_up",
        archive_lead=lead if str(lead.get("call_sid") or "").strip() != str(payload.get("call_sid") or "").strip() else None,
    )
    updates["metadata"] = metadata
    if _should_synthesize_booking_follow_up(metadata, str(payload.get("call_sid") or "").strip() or None):
        summary_lead = dict(lead)
        summary_lead.update(updates)
        issue_summary, call_summary = _synthesize_booking_follow_up_summaries(summary_lead, metadata)
        if issue_summary:
            updates["issue_summary"] = issue_summary
        if call_summary:
            updates["call_summary"] = call_summary
    return update_lead_system_fields_by_id_sync(lead_id, updates)


async def update_business_lead_from_payload_async(lead_id: str | None, payload: dict[str, Any]) -> bool:
    """Async wrapper for business-record updates driven by submit_lead."""
    loop = asyncio.get_event_loop()
    ok = await loop.run_in_executor(None, lambda: update_business_lead_from_payload_sync(lead_id, payload))
    if ok and (Config.LEAD_BACKEND or "webhook").strip().lower() == "supabase":
        from services.call_record_events import notify_call_records_changed_async

        await notify_call_records_changed_async()
    return ok


def list_leads_sync(
    limit: int = 100,
    offset: int = 0,
    priority: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    has_booking: bool | None = None,
    has_address: bool | None = None,
    is_spam: bool | None = None,
    status: str | None = None,
    address: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """
    List leads from Supabase table, newest first, with optional filters.
    Only runs when CALL_RECORD_BACKEND=supabase and Supabase is configured.
    Returns (list of row dicts, total count). On error returns ([], 0).
    Order: created_at desc if column exists, else timestamp desc.
    Filters: priority, date_from/date_to (interpreted in Config.TIMEZONE -> UTC), has_booking, has_address, is_spam (lead_status=spam), status (lead_status), address (general search).
    """
    backend = (Config.LEAD_BACKEND or "webhook").strip().lower()
    if backend != "supabase":
        return [], 0
    if not Config.SUPABASE_URL or not Config.SUPABASE_KEY:
        return [], 0
    table = Config.SUPABASE_LEAD_TABLE or "leads"
    try:
        from supabase import create_client
    except ImportError:
        Log.error("supabase package not installed; pip install supabase")
        return [], 0

    from_iso, to_iso = _date_range_to_utc_iso(date_from, date_to)
    # Use only UTC bounds from parser; never pass raw date strings to DB (created_at is timestamptz/UTC).

    def _run_query(order_col: str):
        query = (
            client.table(table)
            .select("*", count="exact")
            .order(order_col, desc=True)
        )
        if priority and str(priority).strip():
            query = query.eq("priority", str(priority).strip())
        if from_iso:
            query = query.gte(order_col, from_iso)
        if to_iso:
            query = query.lte(order_col, to_iso)
        if has_booking is True:
            try:
                query = query.not_.is_("confirmed_slot", "null")
            except AttributeError:
                pass
        if has_address is True:
            try:
                query = query.not_.is_("service_address", "null").neq("service_address", "")
            except AttributeError:
                try:
                    query = query.not_.is_("service_address", "null")
                except AttributeError:
                    pass
        if is_spam is True:
            try:
                query = query.eq("lead_status", "spam")
            except Exception:
                pass
        if status and str(status).strip():
            query = query.eq("lead_status", str(status).strip().lower())
        if address and str(address).strip():
            term = str(address).strip()
            pattern = "%" + term + "%"
            # General address search: match term in service_address, issue_summary, or call_summary
            try:
                or_filter = (
                    f"service_address.ilike.{pattern},"
                    f"issue_summary.ilike.{pattern},"
                    f"call_summary.ilike.{pattern}"
                )
                query = query.or_(or_filter)
            except Exception:
                try:
                    query = query.ilike("service_address", pattern)
                except Exception:
                    pass
        return query.range(offset, offset + limit - 1).execute()

    try:
        client = create_client(Config.SUPABASE_URL.strip(), Config.SUPABASE_KEY.strip())
        try:
            response = _run_query("created_at")
        except Exception as e:
            err_msg = str(e) if e else ""
            # Column created_at may not exist; fall back to timestamp (handoff payload field)
            if "created_at" in err_msg and ("does not exist" in err_msg or "42703" in err_msg):
                response = _run_query("timestamp")
            else:
                raise
        rows = response.data if hasattr(response, "data") else []
        rows = list(rows) if isinstance(rows, list) else []
        total = getattr(response, "count", None)
        if total is None:
            total = len(rows)
        return rows, int(total)
    except Exception as e:
        Log.error(f"Supabase list leads error: {e}")
        return [], 0


def _deliver_lead_not_implemented_sync(backend: str, payload: dict[str, Any]) -> bool:
    """Log and return False for backends not yet implemented."""
    Log.info(f"CALL_RECORD_BACKEND={backend} not implemented yet; lead not delivered.")
    return False


def send_handoff_sync(payload: dict[str, Any]) -> bool:
    """
    Deliver lead to the configured backend. Returns True on success.
    Supported: webhook (default), supabase. Others (googlesheets, email, airtable, sms, telegram, slack) log and return False until implemented.
    """
    backend = (Config.LEAD_BACKEND or "webhook").strip().lower()
    if backend == "webhook":
        return _deliver_lead_webhook_sync(payload)
    if backend == "supabase":
        return _deliver_lead_supabase_sync(payload)
    if backend in LEAD_BACKEND_NAMES:
        return _deliver_lead_not_implemented_sync(backend, payload)
    Log.error(f"Unknown CALL_RECORD_BACKEND={backend}; use one of: {', '.join(sorted(LEAD_BACKEND_NAMES))}")
    return False


async def deliver_lead_async(payload: dict[str, Any]) -> bool:
    """Deliver lead in a thread so we don't block the event loop."""
    loop = asyncio.get_event_loop()
    ok = await loop.run_in_executor(None, lambda: send_handoff_sync(payload))
    if ok and (Config.LEAD_BACKEND or "webhook").strip().lower() == "supabase":
        from services.call_record_events import notify_call_records_changed_async

        await notify_call_records_changed_async()
    return ok


# Backwards compatibility: send_handoff_async = deliver_lead_async
async def send_handoff_async(payload: dict[str, Any]) -> bool:
    """Alias for deliver_lead_async."""
    return await deliver_lead_async(payload)
