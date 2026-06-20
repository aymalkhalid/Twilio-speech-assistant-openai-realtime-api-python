"""
Outbound campaign service: Supabase CRUD for campaigns and contacts,
campaign type config loading, and system message builder for outbound calls.

Mirrors the patterns in webhook_service.py — sync Supabase functions
called via asyncio.to_thread() from async route handlers.
"""
import os
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from config import Config, _build_accent_instruction, _build_language_instruction
from services.log_utils import Log

from services.outbound_campaign_types import DEFAULT_CAMPAIGN_TYPES

# ---------------------------------------------------------------------------
# Campaign type config
# ---------------------------------------------------------------------------

_campaign_types_cache: dict[str, Any] | None = None


def get_campaign_types() -> dict[str, Any]:
    """Return built-in campaign type definitions. Cached after first load."""
    global _campaign_types_cache
    if _campaign_types_cache is None:
        _campaign_types_cache = dict(DEFAULT_CAMPAIGN_TYPES)
    return _campaign_types_cache



def get_campaign_type_config(campaign_type: str) -> dict[str, Any]:
    """Return config dict for a specific campaign type, or empty dict if not found."""
    return get_campaign_types().get(campaign_type, {})


def _append_language_and_accent_policy(prompt: str) -> str:
    """Append global Realtime language/accent policy to outbound prompts."""
    sections = [prompt.rstrip()]
    if "# Language" not in prompt:
        sections.append(
            _build_language_instruction(
                getattr(Config, "ASSISTANT_LANGUAGE", "English"),
                getattr(Config, "LANGUAGE_SWITCH_POLICY", "explicit_or_substantive"),
            ).strip()
        )
    if "# Accent" not in prompt:
        sections.append(
            _build_accent_instruction(
                getattr(Config, "ASSISTANT_LANGUAGE", "English"),
                getattr(Config, "ASSISTANT_ACCENT", "neutral American"),
                getattr(Config, "ASSISTANT_ACCENT_STRENGTH", "light"),
            ).strip()
        )
    return "\n\n".join(section for section in sections if section)


# ---------------------------------------------------------------------------
# Supabase client helper
# ---------------------------------------------------------------------------

def _get_supabase_client():
    """Create and return a Supabase client. Returns None if not configured."""
    if not Config.SUPABASE_URL or not Config.SUPABASE_KEY:
        Log.info("SUPABASE_URL/SUPABASE_KEY not set; outbound service unavailable")
        return None
    try:
        from supabase import create_client
    except ImportError:
        Log.error("supabase package not installed; pip install supabase")
        return None
    return create_client(Config.SUPABASE_URL.strip(), Config.SUPABASE_KEY.strip())


def _campaigns_table() -> str:
    return Config.SUPABASE_OUTBOUND_CAMPAIGNS_TABLE or "outbound_campaigns"


def _contacts_table() -> str:
    return Config.SUPABASE_OUTBOUND_CONTACTS_TABLE or "outbound_contacts"


# ---------------------------------------------------------------------------
# Campaign CRUD
# ---------------------------------------------------------------------------

def create_campaign_sync(
    name: str,
    campaign_type: str = "general",
    message_template: str = "",
    concurrency: int = 1,
) -> dict[str, Any] | None:
    """Insert a new campaign row. Returns the inserted row dict or None on failure."""
    client = _get_supabase_client()
    if not client:
        return None
    concurrency = max(1, min(concurrency, Config.OUTBOUND_MAX_CONCURRENCY))
    row = {
        "name": name,
        "campaign_type": campaign_type,
        "message_template": message_template,
        "concurrency": concurrency,
        "status": "draft",
    }
    try:
        r = client.table(_campaigns_table()).insert(row).execute()
        data = (r.data or []) if hasattr(r, "data") else []
        if data:
            Log.info(f"Outbound campaign created: {data[0].get('id')}")
            return data[0]
        return None
    except Exception as e:
        Log.error(f"Outbound campaign create error: {e}")
        return None


def list_campaigns_sync(
    limit: int = 50,
    offset: int = 0,
    status: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """List campaigns ordered by created_at DESC. Returns (rows, total_count)."""
    client = _get_supabase_client()
    if not client:
        return [], 0
    try:
        query = (
            client.table(_campaigns_table())
            .select("*", count="exact")
            .order("created_at", desc=True)
        )
        if status and str(status).strip():
            query = query.eq("status", str(status).strip())
        query = query.range(offset, offset + limit - 1)
        r = query.execute()
        data = (r.data or []) if hasattr(r, "data") else []
        total = r.count if hasattr(r, "count") and r.count is not None else len(data)
        return data, total
    except Exception as e:
        Log.error(f"Outbound campaigns list error: {e}")
        return [], 0


def get_campaign_sync(campaign_id: str) -> dict[str, Any] | None:
    """Fetch a single campaign by id, including its contacts."""
    client = _get_supabase_client()
    if not client or not campaign_id:
        return None
    try:
        r = client.table(_campaigns_table()).select("*").eq("id", campaign_id).limit(1).execute()
        data = (r.data or []) if hasattr(r, "data") else []
        if not data:
            return None
        campaign = data[0]
        cr = (
            client.table(_contacts_table())
            .select("*")
            .eq("campaign_id", campaign_id)
            .order("id", desc=False)
            .execute()
        )
        campaign["contacts"] = (cr.data or []) if hasattr(cr, "data") else []
        return campaign
    except Exception as e:
        Log.error(f"Outbound campaign get error: {e}")
        return None


def update_campaign_sync(campaign_id: str, updates: dict[str, Any]) -> bool:
    """Update campaign row by id. Returns True on success."""
    client = _get_supabase_client()
    if not client or not campaign_id or not updates:
        return False
    allowed = {"name", "campaign_type", "message_template", "concurrency", "status", "started_at", "completed_at", "metadata"}
    filtered = {k: v for k, v in updates.items() if k in allowed}
    if not filtered:
        return False
    if "concurrency" in filtered:
        filtered["concurrency"] = max(1, min(int(filtered["concurrency"]), Config.OUTBOUND_MAX_CONCURRENCY))
    try:
        client.table(_campaigns_table()).update(filtered).eq("id", campaign_id).execute()
        Log.info(f"Outbound campaign updated: {campaign_id}")
        return True
    except Exception as e:
        Log.error(f"Outbound campaign update error: {e}")
        return False


def delete_campaign_sync(campaign_id: str) -> bool:
    """Delete campaign by id (cascade deletes contacts). Returns True on success."""
    client = _get_supabase_client()
    if not client or not campaign_id:
        return False
    try:
        client.table(_campaigns_table()).delete().eq("id", campaign_id).execute()
        Log.info(f"Outbound campaign deleted: {campaign_id}")
        return True
    except Exception as e:
        Log.error(f"Outbound campaign delete error: {e}")
        return False


# ---------------------------------------------------------------------------
# Contact CRUD
# ---------------------------------------------------------------------------

def add_contacts_sync(campaign_id: str, contacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Bulk-insert contacts for a campaign. Returns list of inserted rows."""
    client = _get_supabase_client()
    if not client or not campaign_id or not contacts:
        return []
    rows = []
    for c in contacts:
        phone = (c.get("phone") or "").strip()
        if not phone:
            continue
        rows.append({
            "campaign_id": campaign_id,
            "name": (c.get("name") or "").strip(),
            "phone": phone,
            "email": (c.get("email") or "").strip(),
            "custom_fields": c.get("custom_fields") or {},
            "status": "pending",
        })
    if not rows:
        return []
    try:
        r = client.table(_contacts_table()).insert(rows).execute()
        data = (r.data or []) if hasattr(r, "data") else []
        Log.info(f"Outbound contacts added: {len(data)} for campaign {campaign_id}")
        return data
    except Exception as e:
        Log.error(f"Outbound contacts insert error: {e}")
        return []


def delete_contact_sync(contact_id: str) -> bool:
    """Delete a single contact by id. Returns True on success."""
    client = _get_supabase_client()
    if not client or not contact_id:
        return False
    try:
        client.table(_contacts_table()).delete().eq("id", contact_id).execute()
        return True
    except Exception as e:
        Log.error(f"Outbound contact delete error: {e}")
        return False


def update_contact_status_sync(
    contact_id: str,
    status: str,
    call_sid: str | None = None,
    error: str | None = None,
) -> bool:
    """
    Update a contact's call status. Used by the campaign runner and status callbacks.
    Looks up by contact_id when provided, or by call_sid as fallback (Twilio callbacks
    only know the call_sid, not our internal contact_id).
    """
    client = _get_supabase_client()
    if not client:
        return False
    if not contact_id and not call_sid:
        return False
    updates: dict[str, Any] = {"status": status}
    if call_sid is not None:
        updates["call_sid"] = call_sid
    if error is not None:
        updates["error"] = error
    now_iso = datetime.now(timezone.utc).isoformat()
    if status == "calling":
        updates["called_at"] = now_iso
    elif status in ("completed", "failed", "skipped"):
        updates["completed_at"] = now_iso
    try:
        query = client.table(_contacts_table()).update(updates)
        if contact_id:
            query = query.eq("id", contact_id)
        else:
            query = query.eq("call_sid", call_sid)
        query.execute()
        return True
    except Exception as e:
        Log.error(f"Outbound contact status update error: {e}")
        return False


def get_contact_sync(contact_id: str) -> dict[str, Any] | None:
    """Fetch a single contact by id."""
    client = _get_supabase_client()
    if not client or not contact_id:
        return None
    try:
        r = client.table(_contacts_table()).select("*").eq("id", contact_id).limit(1).execute()
        data = (r.data or []) if hasattr(r, "data") else []
        return data[0] if data else None
    except Exception as e:
        Log.error(f"Outbound contact get error: {e}")
        return None


# ---------------------------------------------------------------------------
# Campaign progress
# ---------------------------------------------------------------------------

def get_campaign_progress_sync(campaign_id: str) -> dict[str, int]:
    """Return contact counts grouped by status for a campaign. Used for dashboard progress display."""
    client = _get_supabase_client()
    if not client or not campaign_id:
        return {}
    try:
        r = (
            client.table(_contacts_table())
            .select("status")
            .eq("campaign_id", campaign_id)
            .execute()
        )
        rows = (r.data or []) if hasattr(r, "data") else []
        counts: dict[str, int] = {}
        for row in rows:
            s = row.get("status", "pending")
            counts[s] = counts.get(s, 0) + 1
        counts["total"] = len(rows)
        return counts
    except Exception as e:
        Log.error(f"Outbound campaign progress error: {e}")
        return {}


def reset_failed_to_pending_sync(campaign_id: str) -> int:
    """
    Set all contacts with status 'failed' to 'pending' and clear call_sid, error, completed_at.
    Used when re-running a completed campaign so failed contacts can be redialed.
    Returns the number of contacts reset.
    """
    client = _get_supabase_client()
    if not client or not campaign_id:
        return 0
    try:
        r = (
            client.table(_contacts_table())
            .update({"status": "pending", "call_sid": None, "error": None, "completed_at": None})
            .eq("campaign_id", campaign_id)
            .eq("status", "failed")
            .execute()
        )
        data = (r.data or []) if hasattr(r, "data") else []
        count = len(data)
        if count:
            Log.info(f"Outbound campaign {campaign_id}: reset {count} failed contact(s) to pending")
        return count
    except Exception as e:
        Log.error(f"Outbound reset failed to pending error: {e}")
        return 0


def reset_contact_to_pending_sync(contact_id: str) -> bool:
    """
    Set a single contact to 'pending' and clear call_sid, error, completed_at.
    Use when a contact is stuck in 'calling' (e.g. call failed before Twilio status callback) so the user can retry.
    Only updates when current status is 'calling' or 'failed'. Returns True if updated.
    """
    contact = get_contact_sync(contact_id)
    if not contact or contact.get("status") not in ("calling", "failed"):
        return False
    client = _get_supabase_client()
    if not client:
        return False
    try:
        client.table(_contacts_table()).update(
            {"status": "pending", "call_sid": None, "error": None, "completed_at": None}
        ).eq("id", contact_id).execute()
        Log.info(f"Outbound contact {contact_id} reset to pending")
        return True
    except Exception as e:
        Log.error(f"Outbound contact reset error: {e}")
        return False


# ---------------------------------------------------------------------------
# System message builder for outbound calls
# ---------------------------------------------------------------------------

def build_outbound_system_message(campaign_id: str, contact_id: str) -> str | None:
    """
    Fetch campaign + contact from Supabase and render the message_template
    with contact-specific placeholders. Returns the rendered system message,
    or None if data is missing (caller should fall back to default).
    """
    client = _get_supabase_client()
    if not client:
        return None
    try:
        cr = client.table(_campaigns_table()).select("*").eq("id", campaign_id).limit(1).execute()
        campaign_rows = (cr.data or []) if hasattr(cr, "data") else []
        if not campaign_rows:
            Log.info(f"Outbound system message: campaign {campaign_id} not found")
            return None
        campaign = campaign_rows[0]

        ctr = client.table(_contacts_table()).select("*").eq("id", contact_id).limit(1).execute()
        contact_rows = (ctr.data or []) if hasattr(ctr, "data") else []
        if not contact_rows:
            Log.info(f"Outbound system message: contact {contact_id} not found")
            return None
        contact = contact_rows[0]
    except Exception as e:
        Log.error(f"Outbound system message fetch error: {e}")
        return None

    template = (campaign.get("message_template") or "").strip()
    if not template:
        type_cfg = get_campaign_type_config(campaign.get("campaign_type", "general"))
        template = (type_cfg.get("default_script") or "").strip()
    if not template:
        return None

    from system_instructions import get_agent_name
    agent_name = get_agent_name() or "the voice agent"
    replacements = {
        "contact_name": contact.get("name") or "there",
        "company_name": os.getenv("COMPANY_NAME", "our company"),
        "agent_name": agent_name,
        "receptionist_name": agent_name,
    }
    custom_fields = contact.get("custom_fields") or {}
    if isinstance(custom_fields, dict):
        replacements.update(custom_fields)

    result = template
    for key, value in replacements.items():
        result = result.replace("{" + key + "}", str(value))

    return _append_language_and_accent_policy(result)


# ---------------------------------------------------------------------------
# Campaign runner (async orchestrator)
# ---------------------------------------------------------------------------

async def run_campaign(campaign_id: str, base_url: str) -> None:
    """
    Dial all pending contacts in a campaign with concurrency control.
    Launched as a background task from the /start endpoint.
    Checks campaign status before each dial — if paused, stops picking up new contacts.
    """
    import asyncio
    from services.twilio_service import TwilioService

    campaign = await asyncio.to_thread(get_campaign_sync, campaign_id)
    if not campaign:
        Log.error(f"run_campaign: campaign {campaign_id} not found")
        return

    concurrency = max(1, min(campaign.get("concurrency", 1), Config.OUTBOUND_MAX_CONCURRENCY))
    contacts = campaign.get("contacts") or []
    pending = [c for c in contacts if c.get("status") == "pending"]
    if not pending:
        await asyncio.to_thread(update_campaign_sync, campaign_id, {
            "status": "completed",
            "completed_at": datetime.now(timezone.utc).isoformat(),
        })
        return

    Log.info(f"Outbound campaign {campaign_id}: dialing {len(pending)} contacts (concurrency={concurrency})")
    sem = asyncio.Semaphore(concurrency)

    async def _dial_one(contact: dict) -> None:
        async with sem:
            fresh = await asyncio.to_thread(get_campaign_sync, campaign_id)
            if not fresh or fresh.get("status") != "running":
                Log.info(f"Campaign {campaign_id} no longer running; skipping contact {contact.get('id')}")
                return

            contact_id = contact["id"]
            phone = (contact.get("phone") or "").strip()
            if not phone:
                await asyncio.to_thread(update_contact_status_sync, contact_id, "skipped", error="No phone number")
                return

            await asyncio.to_thread(update_contact_status_sync, contact_id, "calling")
            twiml_url = f"{base_url}/outbound-call-twiml/{campaign_id}?contact_id={quote(contact_id, safe='')}"
            status_callback = f"{base_url}/outbound-call-status"

            try:
                call = await TwilioService.create_outbound_call(
                    to=phone,
                    twiml_url=twiml_url,
                    status_callback=status_callback,
                )
                TwilioService.register_outbound_context(call.sid, campaign_id, contact_id)
                await asyncio.to_thread(
                    update_contact_status_sync, contact_id, "calling", call_sid=call.sid
                )
            except Exception as e:
                Log.error(f"Outbound dial failed for {phone}: {e}")
                await asyncio.to_thread(
                    update_contact_status_sync, contact_id, "failed", error=str(e)[:500]
                )

    tasks = [_dial_one(c) for c in pending]
    await asyncio.gather(*tasks, return_exceptions=True)

    final = await asyncio.to_thread(get_campaign_sync, campaign_id)
    if final and final.get("status") == "running":
        await asyncio.to_thread(update_campaign_sync, campaign_id, {
            "status": "completed",
            "completed_at": datetime.now(timezone.utc).isoformat(),
        })
        Log.info(f"Outbound campaign {campaign_id} completed")
