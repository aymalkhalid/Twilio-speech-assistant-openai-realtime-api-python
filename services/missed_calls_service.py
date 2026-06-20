"""
Missed calls service.

Pulls inbound calls from Twilio that went unanswered or reached our AI but
produced no call record in Supabase, so the dashboard can show them and let
the user call back (with the AI or from their own phone) or mark handled.

Sync functions are called from async route handlers via asyncio.to_thread,
mirroring the pattern in services/outbound_service.py and
services/webhook_service.py.

Definition of a missed call (see docs/missed-calls/):
  1. Twilio inbound call with status in {no-answer, busy, failed, canceled}, OR
  2. Twilio inbound call with status=completed that has no Supabase call-record row
     for its call_sid (caller hung up on the AI before save_call_record ran).

Handled calls are excluded: a call-record row for that call_sid with
status "missed_handled" hides the call from the list.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from config import Config
from services.log_utils import Log
from services.call_records_service import (
    get_call_record_by_call_sid_sync,
    update_call_record_by_call_sid_sync,
)

# Campaign used to dial AI callbacks to missed calls. Created on first use.
MISSED_CALL_CALLBACK_CAMPAIGN_NAME = "__missed_call_callbacks__"
MISSED_CALL_CALLBACK_CAMPAIGN_TYPE = "missed_call_callback"

# Storage-schema status sentinel set by "Mark handled".
MISSED_HANDLED_STATUS = "missed_handled"

# Twilio call statuses that indicate we did not connect the caller to the AI.
_MISSED_STATUSES = frozenset({"no-answer", "busy", "failed", "canceled"})
_INBOUND_DIRECTIONS = frozenset({"inbound", "inbound-api", "inbound-sip-api"})


# ---------------------------------------------------------------------------
# Supabase helper (shared across helpers in this module)
# ---------------------------------------------------------------------------

def _get_supabase_client():
    """Create and return a Supabase client. Returns None if not configured."""
    if not Config.SUPABASE_URL or not Config.SUPABASE_KEY:
        return None
    try:
        from supabase import create_client
    except ImportError:
        Log.error("supabase package not installed; pip install supabase")
        return None
    return create_client(Config.SUPABASE_URL.strip(), Config.SUPABASE_KEY.strip())


# ---------------------------------------------------------------------------
# Twilio lookup
# ---------------------------------------------------------------------------

def _twilio_inbound_calls(since_hours: int, limit: int) -> list[dict[str, Any]]:
    """
    Fetch recent inbound calls to the business number (TWILIO_OUTBOUND_NUMBER)
    from the Twilio Calls API. Returns a normalized list of dicts.
    """
    if not Config.has_twilio_credentials():
        return []
    to_number = (Config.TWILIO_OUTBOUND_NUMBER or "").strip()
    if not to_number:
        Log.info("Missed calls: TWILIO_OUTBOUND_NUMBER not set; cannot filter inbound calls")
        return []
    try:
        from twilio.rest import Client
    except ImportError:
        Log.error("twilio package not installed; pip install twilio")
        return []
    hours = max(1, min(int(since_hours or 72), 24 * 30))
    start_after = datetime.now(timezone.utc) - timedelta(hours=hours)
    page_size = max(1, min(int(limit or 50), 200))
    try:
        client = Client(Config.TWILIO_ACCOUNT_SID, Config.TWILIO_AUTH_TOKEN)
        calls = client.calls.list(
            to=to_number,
            start_time_after=start_after,
            limit=page_size,
        )
    except Exception as e:
        Log.error(f"Missed calls: Twilio calls.list error: {e}")
        return []

    result: list[dict[str, Any]] = []
    for c in calls or []:
        direction = (getattr(c, "direction", "") or "").strip().lower()
        if direction and direction not in _INBOUND_DIRECTIONS:
            continue
        start_time = getattr(c, "start_time", None)
        if isinstance(start_time, datetime):
            start_iso = start_time.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        else:
            start_iso = str(start_time or "")
        try:
            duration = int(getattr(c, "duration", None) or 0)
        except (TypeError, ValueError):
            duration = 0
        result.append({
            "call_sid": getattr(c, "sid", "") or "",
            "from_number": getattr(c, "from_", None) or getattr(c, "from_formatted", "") or "",
            "to_number": getattr(c, "to", "") or "",
            "start_time": start_iso,
            "status": (getattr(c, "status", "") or "").strip().lower(),
            "duration_sec": duration,
            "direction": direction,
        })
    return result


# ---------------------------------------------------------------------------
# Call-record cross-reference
# ---------------------------------------------------------------------------

def _call_record_status_by_call_sid(call_sids: list[str]) -> dict[str, str | None]:
    """
    Return {call_sid: status or None} for every sid that has a call-record row.
    Missing sids (no call-record row) are not included in the dict.

    Only queries when CALL_RECORD_BACKEND=supabase is configured.
    """
    backend = (Config.CALL_RECORD_BACKEND or "webhook").strip().lower()
    if backend != "supabase":
        return {}
    sids = [s for s in call_sids if s]
    if not sids:
        return {}
    client = _get_supabase_client()
    if not client:
        return {}
    table = Config.SUPABASE_CALL_RECORD_TABLE or "leads"
    try:
        r = (
            client.table(table)
            .select("call_sid, lead_status")
            .in_("call_sid", sids)
            .execute()
        )
        rows = (r.data or []) if hasattr(r, "data") else []
    except Exception as e:
        Log.error(f"Missed calls: Supabase call-record lookup error: {e}")
        return {}
    out: dict[str, str | None] = {}
    for row in rows:
        sid = (row.get("call_sid") or "").strip()
        if sid:
            out[sid] = (row.get("lead_status") or None)
    return out


# ---------------------------------------------------------------------------
# Public: list missed calls
# ---------------------------------------------------------------------------

def list_missed_calls_sync(since_hours: int = 72, limit: int = 50) -> list[dict[str, Any]]:
    """
    Return a list of missed calls (newest first).

    Each item:
      {
        "call_sid": "...",
        "from_number": "+1...",
        "to_number": "+1...",
        "start_time": "2026-04-19T12:34:56Z",
        "status": "no-answer" | "busy" | "failed" | "canceled" | "completed",
        "duration_sec": 0,
        "reason": "no-answer" | "busy" | "failed" | "canceled" | "no_call_record_captured",
      }
    """
    calls = _twilio_inbound_calls(since_hours=since_hours, limit=limit)
    if not calls:
        return []

    # Bucket sids so we only query Supabase for the ones we might need.
    missed_status_calls: list[dict[str, Any]] = []
    completed_calls: list[dict[str, Any]] = []
    for c in calls:
        status = c.get("status") or ""
        if status in _MISSED_STATUSES:
            missed_status_calls.append(c)
        elif status == "completed":
            completed_calls.append(c)

    # We need call-record rows for every sid (missed + completed) so we can hide
    # items the user already marked handled. One round trip.
    all_sids = [c["call_sid"] for c in (missed_status_calls + completed_calls) if c.get("call_sid")]
    status_by_sid = _call_record_status_by_call_sid(all_sids)

    result: list[dict[str, Any]] = []
    for c in missed_status_calls:
        if status_by_sid.get(c["call_sid"]) == MISSED_HANDLED_STATUS:
            continue
        result.append({**c, "reason": c["status"]})
    for c in completed_calls:
        sid = c["call_sid"]
        lead_status = status_by_sid.get(sid)
        if lead_status == MISSED_HANDLED_STATUS:
            continue
        # Caller reached the AI but no call record exists => caller hung up early.
        if sid not in status_by_sid:
            result.append({**c, "reason": "no_call_record_captured"})

    result.sort(key=lambda x: x.get("start_time") or "", reverse=True)
    return result


# ---------------------------------------------------------------------------
# Public: mark handled
# ---------------------------------------------------------------------------

def mark_handled_sync(call_sid: str, caller_number: str | None = None) -> bool:
    """
    Mark a missed call as handled by upserting a call-record row with status="missed_handled".

    - If a call-record row already exists for call_sid, update it in place.
    - Otherwise insert a minimal compatibility row (lead_phone, call_sid, lead_status, source in metadata).

    Only effective when CALL_RECORD_BACKEND=supabase; for other backends the missed-call list
    is already served entirely from Twilio and "Mark handled" cannot be persisted.
    """
    call_sid = (str(call_sid).strip() if call_sid else "")
    if not call_sid:
        return False

    backend = (Config.CALL_RECORD_BACKEND or "webhook").strip().lower()
    if backend != "supabase":
        Log.info(f"Missed calls: mark_handled skipped (CALL_RECORD_BACKEND={backend}, not supabase)")
        return False

    existing = get_call_record_by_call_sid_sync(call_sid)
    if existing:
        return update_call_record_by_call_sid_sync(call_sid, {"lead_status": MISSED_HANDLED_STATUS})

    client = _get_supabase_client()
    if not client:
        return False
    table = Config.SUPABASE_CALL_RECORD_TABLE or "leads"
    row = {
        "company_name": Config.COMPANY_NAME,
        "industry": Config.AGENT_LABEL,
        "agent_label": Config.AGENT_LABEL,
        "call_sid": call_sid,
        "lead_phone": (caller_number or "").strip() or None,
        "lead_status": MISSED_HANDLED_STATUS,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "metadata": {"source": "missed_call"},
    }
    try:
        client.table(table).insert(row).execute()
        Log.info(f"Missed call marked handled (insert): {call_sid}")
        return True
    except Exception as e:
        Log.error(f"Missed calls: insert handled call-record error: {e}")
        return False


# ---------------------------------------------------------------------------
# Singleton callback campaign (used by "Call back with AI")
# ---------------------------------------------------------------------------

def get_or_create_callback_campaign_sync() -> dict[str, Any] | None:
    """
    Return the singleton "missed call callbacks" campaign, creating it if missing.
    Used by the callback-ai route so we can reuse the full outbound dial pipeline
    (TwiML endpoint, media stream, system-message builder, status callback).
    """
    client = _get_supabase_client()
    if not client:
        return None
    table = Config.SUPABASE_OUTBOUND_CAMPAIGNS_TABLE or "outbound_campaigns"
    try:
        r = (
            client.table(table)
            .select("*")
            .eq("name", MISSED_CALL_CALLBACK_CAMPAIGN_NAME)
            .limit(1)
            .execute()
        )
        rows = (r.data or []) if hasattr(r, "data") else []
        if rows:
            return rows[0]
    except Exception as e:
        Log.error(f"Missed calls: lookup callback campaign error: {e}")
        return None

    # Not found — create it.
    from services.outbound_service import create_campaign_sync
    campaign = create_campaign_sync(
        name=MISSED_CALL_CALLBACK_CAMPAIGN_NAME,
        campaign_type=MISSED_CALL_CALLBACK_CAMPAIGN_TYPE,
        message_template="",
        concurrency=1,
    )
    return campaign


def finalize_callback_if_missed_sync(outbound_call_sid: str, is_completed: bool) -> dict[str, Any]:
    """
    Post-call hook for the Twilio status callback: if this outbound call came from
    a missed-call callback (contact has custom_fields.missed_call_sid), then:

    - On completed: mark the original missed call as handled so it disappears
      from the missed-calls list on next fetch.
    - Always: append a note to the new lead row (by outbound call_sid) linking
      it back to the original missed-call SID so users can see the provenance
      in the Leads dashboard.

    Returns a small dict describing what was done (for logging); silent no-op
    when the call is not a missed-call callback.
    """
    result: dict[str, Any] = {"is_missed_callback": False}
    outbound_call_sid = (outbound_call_sid or "").strip()
    if not outbound_call_sid:
        return result

    client = _get_supabase_client()
    if not client:
        return result

    contacts_table = Config.SUPABASE_OUTBOUND_CONTACTS_TABLE or "outbound_contacts"
    try:
        r = (
            client.table(contacts_table)
            .select("id, campaign_id, phone, custom_fields")
            .eq("call_sid", outbound_call_sid)
            .limit(1)
            .execute()
        )
        rows = (r.data or []) if hasattr(r, "data") else []
    except Exception as e:
        Log.error(f"Missed calls: finalize lookup error: {e}")
        return result
    if not rows:
        return result
    contact = rows[0]
    custom_fields = contact.get("custom_fields") or {}
    original_sid = ""
    if isinstance(custom_fields, dict):
        original_sid = (custom_fields.get("missed_call_sid") or "").strip()
    if not original_sid:
        return result
    result["is_missed_callback"] = True
    result["original_call_sid"] = original_sid

    if is_completed:
        marked = mark_handled_sync(original_sid, caller_number=contact.get("phone"))
        result["marked_handled"] = bool(marked)

    # Append a provenance note to the lead captured during the callback (if any).
    try:
        from services.call_records_service import append_call_record_note_by_call_sid_sync
        note = f"Outbound AI callback for missed call {original_sid}."
        appended = append_call_record_note_by_call_sid_sync(outbound_call_sid, note)
        result["note_appended"] = bool(appended)
    except Exception as e:
        Log.error(f"Missed calls: note append error: {e}")

    Log.event("Missed-call callback finalized", result)
    return result


def add_callback_contact_sync(campaign_id: str, call_sid: str, phone: str) -> dict[str, Any] | None:
    """
    Insert a single contact row into the callback campaign for this missed call.
    Stores the original call_sid in custom_fields for traceability.
    """
    phone = (phone or "").strip()
    if not (campaign_id and phone):
        return None
    client = _get_supabase_client()
    if not client:
        return None
    table = Config.SUPABASE_OUTBOUND_CONTACTS_TABLE or "outbound_contacts"
    row = {
        "campaign_id": campaign_id,
        "name": "",
        "phone": phone,
        "email": "",
        "custom_fields": {"missed_call_sid": call_sid or ""},
        "status": "pending",
    }
    try:
        r = client.table(table).insert(row).execute()
        data = (r.data or []) if hasattr(r, "data") else []
        return data[0] if data else None
    except Exception as e:
        Log.error(f"Missed calls: insert callback contact error: {e}")
        return None
