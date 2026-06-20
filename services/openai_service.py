import json
import asyncio
import time
import os
import re
from difflib import SequenceMatcher
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from config import Config
from services.log_utils import Log

try:
    from system_instructions import (
        get_agent_name as get_agent_name,
        get_farewell_instruction,
        get_greeting_instruction,
    )
except ImportError:
    def get_greeting_instruction(company_name: str | None = None) -> str:
        return "Greet the user and ask how you can help."
    def get_farewell_instruction(company_name=None, reason=None, *, call_record_saved=False, lead_submitted=False, appointment_booked=False, confirmed_slot_display=None, priority=None):
        return "Say a brief, polite goodbye. Do not call any tools; speak now."
    def get_agent_name() -> str:
        return ""


try:
    from services.google_calendar_booking_service import get_availability as booking_get_availability
    from services.google_calendar_booking_service import book_appointment as booking_book_appointment
    from services.google_calendar_booking_service import list_my_bookings as booking_list_my_bookings
    from services.google_calendar_booking_service import delete_booking as booking_delete_booking
    from services.google_calendar_booking_service import edit_booking as booking_edit_booking
    from services.google_calendar_booking_service import is_booking_enabled
except ImportError:
    def booking_get_availability(*a, **k):
        return []
    def booking_book_appointment(*a, **k):
        return {"success": False, "message": "Booking not configured.", "event_id": None, "confirmed_slot": None}
    def booking_list_my_bookings(*a, **k):
        return []
    def booking_delete_booking(*a, **k):
        return {"success": False, "message": "Booking not configured."}
    def booking_edit_booking(*a, **k):
        return {"success": False, "message": "Booking not configured.", "confirmed_slot": None}
    def is_booking_enabled():
        return False

try:
    from services.call_records_service import has_call_record_backend_configured
except ImportError:
    def has_call_record_backend_configured() -> bool:
        return False

try:
    from services.tool_registry import external_tool_registry
    from services.mcp_adapter import load_mcp_tools
except ImportError:
    external_tool_registry = None

    def load_mcp_tools(registry) -> None:
        return None


def _is_placeholder_phone(s: str) -> bool:
    """True if s looks like a placeholder (e.g. \"caller's number\") rather than a real number."""
    if not s:
        return True
    t = s.lower().strip()
    if any(c.isdigit() for c in t):
        return False
    return "caller" in t and ("number" in t or "phone" in t)


_KNOWN_PROMPT_EXAMPLE_PHONE_DIGITS = {"2185953061"}

_CALLER_PHONE_CONTEXT_PHRASES = (
    "number they are calling from",
    "number they re calling from",
    "number the caller is calling from",
    "number caller is calling from",
    "number they called from",
    "number the caller called from",
    "calling from this number",
    "called from this number",
    "same number",
    "this number",
    "current number",
    "caller id",
    "caller phone",
    "caller_phone",
    "from context",
)

_ALTERNATE_PHONE_CONTEXT_PHRASES = (
    "different number",
    "alternate number",
    "another number",
    "new number",
    "provided number",
    "number they provided",
    "not the number",
    "not caller id",
)


def _phone_digits_for_match(value: Any) -> str:
    """Return a canonical digit string for comparing US phone values."""
    digits = re.sub(r"\D+", "", str(value or ""))
    if len(digits) == 11 and digits.startswith("1"):
        return digits[1:]
    return digits


def _phone_values_match(left: Any, right: Any) -> bool:
    left_digits = _phone_digits_for_match(left)
    right_digits = _phone_digits_for_match(right)
    return bool(left_digits and right_digits and left_digits == right_digits)


def _phone_context_text(args: Dict[str, Any]) -> str:
    parts = []
    for key in (
        "issue_summary",
        "call_summary",
        "preferred_callback_time",
        "summary",
        "reason",
    ):
        value = args.get(key)
        if value:
            parts.append(str(value))
    return " ".join(parts)


def _normalized_phone_context_text(value: Any) -> str:
    return re.sub(r"[^a-z0-9_+]+", " ", str(value or "").lower()).strip()


def _text_mentions_alternate_phone(value: Any) -> bool:
    text = _normalized_phone_context_text(value)
    return any(phrase in text for phrase in _ALTERNATE_PHONE_CONTEXT_PHRASES)


def _text_requests_caller_phone(value: Any) -> bool:
    text = _normalized_phone_context_text(value)
    if _text_mentions_alternate_phone(text):
        return False
    return any(phrase in text for phrase in _CALLER_PHONE_CONTEXT_PHRASES)


def _caller_phone_override_reason(
    raw_phone: Any,
    caller_phone: str,
    args: Dict[str, Any],
) -> str | None:
    """
    Return a reason to replace a model-supplied phone with caller_phone.

    The guard is intentionally narrow: preserve real alternate callback numbers,
    but correct prompt-example leakage and "use the number they called from" cases.
    """
    p = str(raw_phone or "").strip()
    if not p or _is_placeholder_phone(p):
        return "empty_or_placeholder"
    if not caller_phone or _phone_values_match(p, caller_phone):
        return None

    context_text = _phone_context_text(args)
    if _text_requests_caller_phone(context_text):
        return "tool_context_requests_caller_phone"

    if (
        _phone_digits_for_match(p) in _KNOWN_PROMPT_EXAMPLE_PHONE_DIGITS
        and not _text_mentions_alternate_phone(context_text)
    ):
        return "known_prompt_example_phone"

    return None


_BOOKING_MATCH_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "for",
        "to",
        "of",
        "my",
        "me",
        "appointment",
        "booked",
        "booking",
        "caller",
        "under",
        "at",
        "on",
        "today",
        "tomorrow",
        "please",
    }
)


def _normalize_match_text(value: Any) -> str:
    """Lowercase alphanumeric text with spaces normalized for deterministic local matching."""
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _tokenize_match_text(value: Any) -> list[str]:
    """Tokenize text for overlap scoring while dropping low-signal filler words."""
    return [
        token
        for token in _normalize_match_text(value).split()
        if token and token not in _BOOKING_MATCH_STOPWORDS
    ]


def _extract_time_aliases(value: Any) -> set[str]:
    """Extract lightweight aliases like '8am' and '8:00 am' from booking text or caller hints."""
    aliases: set[str] = set()
    text = str(value or "").lower()
    for match in re.finditer(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", text):
        hour = int(match.group(1))
        minute = match.group(2) or "00"
        ampm = match.group(3)
        if not 1 <= hour <= 12:
            continue
        aliases.add(f"{hour}{ampm}")
        aliases.add(f"{hour} {ampm}")
        aliases.add(f"{hour}:{minute} {ampm}")
    return aliases


def _booking_field_match_reason(field_label: str, field_value: Any, hint_tokens: set[str]) -> tuple[int, str | None]:
    """Return (score, reason) when caller hint tokens overlap a booking field."""
    if not hint_tokens:
        return 0, None
    field_tokens = set(_tokenize_match_text(field_value))
    overlap = field_tokens & hint_tokens
    if not overlap:
        return 0, None
    sample = ", ".join(sorted(overlap)[:3])
    return 3, f"{field_label} match ({sample})"


def _name_similarity_score(left: Any, right: Any) -> float:
    """
    Return a lightweight fuzzy similarity score for booked-under names.

    This is only a soft ranking signal for clarification, not an ownership check.
    """
    left_norm = _normalize_match_text(left)
    right_norm = _normalize_match_text(right)
    if not left_norm or not right_norm:
        return 0.0
    left_joined = left_norm.replace(" ", "")
    right_joined = right_norm.replace(" ", "")
    if not left_joined or not right_joined:
        return 0.0

    full_ratio = SequenceMatcher(None, left_joined, right_joined).ratio()
    token_ratio = 0.0
    left_tokens = [token for token in left_norm.split() if token]
    right_tokens = [token for token in right_norm.split() if token]
    for left_token in left_tokens:
        for right_token in right_tokens:
            token_ratio = max(
                token_ratio,
                SequenceMatcher(None, left_token, right_token).ratio(),
            )
    return max(full_ratio, token_ratio)


def _rank_booking_candidates(
    bookings: list[dict[str, Any]],
    *,
    contact_name: str | None = None,
    booking_hint: str | None = None,
) -> list[dict[str, Any]]:
    """
    Rank booking candidates locally using deterministic signals only.

    This helps the AI clarify the likely booking before edit/delete without changing
    the strict backend ownership rules enforced by the calendar service.
    """
    caller_name_norm = _normalize_match_text(contact_name)
    hint_tokens = set(_tokenize_match_text(booking_hint))
    hint_time_aliases = _extract_time_aliases(booking_hint)
    ranked: list[dict[str, Any]] = []

    for original_index, booking in enumerate(bookings or []):
        score = 0
        reasons: list[str] = []
        booked_under = str(booking.get("caller_name") or "").strip()
        booked_under_norm = _normalize_match_text(booked_under)
        if caller_name_norm and booked_under_norm:
            if caller_name_norm == booked_under_norm:
                score += 6
                reasons.append("caller name matches booked-under name")
            else:
                fuzzy_score = _name_similarity_score(caller_name_norm, booked_under_norm)
                if fuzzy_score >= 0.74 and min(len(caller_name_norm.replace(" ", "")), len(booked_under_norm.replace(" ", ""))) >= 4:
                    score += 2
                    reasons.append(f"booked-under name is a close variant ({booked_under})")
                else:
                    reasons.append(f"booked under a different name ({booked_under})")

        display = str(booking.get("display") or booking.get("start") or "").strip()
        display_time_aliases = _extract_time_aliases(display)
        if hint_time_aliases and display_time_aliases and (hint_time_aliases & display_time_aliases):
            score += 5
            reasons.append("requested time match")

        for label, key in (
            ("summary", "summary"),
            ("visit details", "visit_summary"),
            ("service type", "service_type"),
            ("booked-under name", "caller_name"),
        ):
            delta, reason = _booking_field_match_reason(label, booking.get(key), hint_tokens)
            if delta:
                score += delta
                if reason:
                    reasons.append(reason)

        candidate = dict(booking)
        candidate["_candidate_score"] = score
        candidate["_candidate_reasons"] = reasons
        candidate["_candidate_original_index"] = original_index
        ranked.append(candidate)

    return sorted(
        ranked,
        key=lambda item: (
            -int(item.get("_candidate_score") or 0),
            str(item.get("start") or ""),
            int(item.get("_candidate_original_index") or 0),
        ),
    )


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


def _effective_booking_days_tokens() -> list[str] | None:
    """
    Effective booking weekdays from runtime config (env/overrides).
    None means all days are enabled (backward-compatible behavior).
    """
    raw = (
        getattr(Config, "BOOKING_DAYS_ENABLED", None)
        or os.getenv("BOOKING_DAYS_ENABLED", "")
    )
    text = str(raw or "").strip().lower()
    if not text:
        return None
    tokens = [p.strip() for p in text.split(",") if p.strip()]
    valid = [p for p in tokens if p in _WEEKDAY_NAMES]
    if not valid:
        return None
    wanted = set(valid)
    return [d for d in _WEEKDAY_NAMES if d in wanted]


def _effective_booking_timezone() -> str:
    """Effective booking timezone from runtime config."""
    return (getattr(Config, "TIMEZONE", None) or os.getenv("TIMEZONE", "America/Los_Angeles") or "America/Los_Angeles").strip()



def _booking_runtime_policy_hint(now_utc: datetime | None = None) -> str:
    """
    Compact runtime policy block for the model so it can answer "open today?"
    consistently using effective settings (including Supabase overrides).
    """
    if not is_booking_enabled():
        return ""
    tz_name = _effective_booking_timezone()
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc
        tz_name = "UTC"

    now_ref = now_utc or datetime.now(timezone.utc)
    if now_ref.tzinfo is None:
        now_ref = now_ref.replace(tzinfo=timezone.utc)
    local_now = now_ref.astimezone(tz)
    weekday_token = _WEEKDAY_NAMES[local_now.weekday()]
    days_enabled = _effective_booking_days_tokens()
    today_bookable = True if days_enabled is None else (weekday_token in days_enabled)
    days_text = "all" if days_enabled is None else ",".join(days_enabled)
    opening = (getattr(Config, "BUSINESS_APPOINTMENT_OPENING_TIME", None) or os.getenv("BUSINESS_APPOINTMENT_OPENING_TIME", "08:00")).strip()
    closing = (getattr(Config, "BUSINESS_APPOINTMENT_CLOSING_TIME", None) or os.getenv("BUSINESS_APPOINTMENT_CLOSING_TIME", "18:00")).strip()
    today_date = local_now.strftime("%Y-%m-%d")

    return (
        "BOOKING_POLICY_RUNTIME: "
        f"timezone={tz_name}; "
        f"opening_24h={opening}; "
        f"closing_24h={closing}; "
        f"enabled_days={days_text}; "
        f"today={today_date}({weekday_token}); "
        f"today_bookable={'true' if today_bookable else 'false'}. "
        "Use this as source of truth. Never say 'open today for booking' when today_bookable=false."
    )


def _format_weekday_tokens(tokens: list[str] | tuple[str, ...] | None) -> str:
    """Render weekday tokens (mon..sun) as a stable, human-readable phrase."""
    if not tokens:
        return ""
    wanted = {str(t).strip().lower() for t in tokens if str(t).strip()}
    labels = [_WEEKDAY_LABELS[d] for d in _WEEKDAY_NAMES if d in wanted]
    if not labels:
        return ""
    if len(labels) == 1:
        return labels[0]
    if len(labels) == 2:
        return f"{labels[0]} and {labels[1]}"
    return ", ".join(labels[:-1]) + f", and {labels[-1]}"


class OpenAIEventHandler:
    """
    Interprets and processes events received from the OpenAI Realtime API.
    
    - Determines which events should be logged.
    - Identifies and extracts audio deltas, speech start events, and item IDs from event payloads.
    
    Used by higher-level services to make sense of incoming OpenAI events and route them appropriately.
    """
    
    @staticmethod
    def should_log_event(event_type: str) -> bool:
        """Check if an event type should be logged."""
        return event_type in Config.LOG_EVENT_TYPES
    
    @staticmethod
    def is_audio_delta_event(event: Dict[str, Any]) -> bool:
        """Check if event is an audio delta from OpenAI."""
        return (event.get('type') == 'response.output_audio.delta' and 
                'delta' in event)
    
    @staticmethod
    def is_speech_started_event(event: Dict[str, Any]) -> bool:
        """Check if event indicates user speech has started."""
        return event.get('type') == 'input_audio_buffer.speech_started'
    
    @staticmethod
    def extract_audio_delta(event: Dict[str, Any]) -> Optional[str]:
        """Extract audio delta from OpenAI event."""
        if OpenAIEventHandler.is_audio_delta_event(event):
            return event.get('delta')
        return None
    
    @staticmethod
    def extract_item_id(event: Dict[str, Any]) -> Optional[str]:
        """Extract item ID from OpenAI event."""
        return event.get('item_id')


class OpenAISessionManager:
    """
    Configures and initializes OpenAI Realtime API sessions.
    
    - Generates session update messages specifying model, audio formats, and system instructions.
    - Creates the initial conversation item (for AI-first greetings) and triggers responses.
    
    Ensures consistent and correct session setup for all OpenAI interactions.
    """
    
    @staticmethod
    def _realtime_tools() -> list:
        """Build the list of tools for the Realtime session (end_call, save_call_record, booking tools)."""
        tools = [
            {
                "type": "function",
                "name": "wait_for_user",
                "description": (
                    "Call this when the latest audio does not need a spoken response, such as silence, background noise, "
                    "hold music, TV audio, side conversation, or speech not addressed to the assistant. "
                    "This tool helps end the turn without a spoken reply."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            },
            {
                "type": "function",
                "name": "end_call",
                "description": "Politely end the phone call only when the caller explicitly says goodbye, says they are done, or requests to end the conversation. Do not use after save_call_record or booking unless the caller has ended the call.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "reason": {"type": "string", "description": "Brief reason for ending, e.g., user said bye."}
                    },
                    "required": []
                }
            }
        ]
        if has_call_record_backend_configured():
            tools.append({
                "type": "function",
                "name": "save_call_record",
                "description": (
                    "Save a call record for follow-up, CRM, webhook delivery, or dashboard tracking. "
                    "Call when you have collected enough information for a useful record. "
                    "Include contact details when available, the reason for the call, priority, and a 2-3 sentence call_summary. "
                    "A short preamble is appropriate before this call when saving may take noticeable time. "
                    "Do not tell the caller the record was saved until the tool result succeeds.\n\n"
                    "Preamble sample phrases:\n"
                    "- I'll save that for the team to follow up.\n"
                    "- I'll note that down for the team now.\n"
                    "- I'll record those details so someone can get back to you."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "contact_name": {"type": "string", "description": "Caller's name"},
                        "contact_phone": {"type": "string", "description": "Callback phone number. Use the actual number from context (caller_phone), not the literal phrase \"caller's number\". If the caller gives the number verbally, confirm it digit by digit before calling."},
                        "contact_email": {"type": "string", "description": "Email if provided. Confirm character by character when precision matters."},
                        "issue_summary": {"type": "string", "description": "Brief description covering the reason for the call. Include multiple topics or bookings when relevant."},
                        "priority": {"type": "string", "description": "Urgency or importance: high/normal, emergency/same_day/routine, or similar."},
                        "call_summary": {"type": "string", "description": "2-3 sentence summary of the entire call so far; include all main topics and all bookings (e.g. both appointments), not only the latest."},
                        "preferred_callback_time": {"type": "string", "description": "When they prefer to be contacted"},
                        "service_address": {"type": "string", "description": "Address or location if relevant and provided."},
                        "confirmed_slot": {"type": "object", "description": "If an appointment was booked: {start, end, display}"}
                    },
                    "required": ["contact_name", "contact_phone", "issue_summary", "priority", "call_summary"]
                }
            })
        if is_booking_enabled():
            tools.append({
                "type": "function",
                "name": "get_availability",
                "description": (
                    "Read-only lookup: get available appointment slots grouped by day and by time of day (morning/afternoon/evening). "
                    "Call proactively when scheduling intent is clear and required fields are available. "
                    "This lookup may take noticeable time; say a short preamble in the same turn before calling. "
                    "Returns a full week of options so you can answer 'today', 'tomorrow', or a specific date. "
                    "Use for_date when the caller asks for a specific date (e.g. 'What's available on March 5?'). "
                    "If a specific weekday is disabled by booking settings, treat that day as closed and tell the caller it is closed (not just 'no slots'). "
                    "After book_appointment, edit_booking, or delete_booking succeeds, call this again if they want more or different times—"
                    "do not reuse an earlier slot list from the conversation; availability may have changed.\n\n"
                    "Preamble sample phrases:\n"
                    "- I'll check availability now.\n"
                    "- I'll look up open times for you.\n"
                    "- I'll see what appointments we have open."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "days_ahead": {"type": "integer", "description": "Number of days to look ahead (default 7). Use when showing general week availability."},
                        "for_date": {"type": "string", "description": "Specific date in YYYY-MM-DD format. Use when caller asks for one day only (e.g. 'March 5' -> 2025-03-05)."}
                    },
                    "required": []
                }
            })
            tools.append({
                "type": "function",
                "name": "book_appointment",
                "description": (
                    "Book an appointment for the chosen slot. Call once after the caller picks a slot from get_availability "
                    "and agrees in conversation (restate day/date/time briefly, get a clear yes—then call this tool). "
                    "Do not call this tool until they have confirmed the slot in natural dialogue. "
                    "This write action may take noticeable time; a short preamble in the same turn is appropriate after confirmation. "
                    "Do not say the appointment is booked until this tool succeeds. "
                    "If they book again or ask for other times later in the call, call get_availability again first.\n\n"
                    "Preamble sample phrases:\n"
                    "- I'll book that appointment now.\n"
                    "- I'll get that time reserved for you.\n"
                    "- I'll confirm that booking now."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "slot_start_iso": {"type": "string", "description": "Start time of the chosen slot (exact ISO value from get_availability). Do not invent or paraphrase the time."},
                        "contact_name": {"type": "string", "description": "Caller's name"},
                        "contact_phone": {"type": "string", "description": "Callback phone. Use the actual number from context (caller_phone), not the literal phrase \"caller's number\"."},
                        "contact_email": {"type": "string", "description": "Email if provided. Confirm character by character when precision matters."},
                        "summary": {"type": "string", "description": "Brief reason for visit"},
                        "confirm_exact_slot": {"type": "boolean", "description": "Optional; ignored by the server. You may set true after verbal confirmation for logging/clarity."}
                    },
                    "required": ["slot_start_iso", "contact_name", "contact_phone"]
                }
            })
            tools.append({
                "type": "function",
                "name": "list_my_bookings",
                "description": (
                    "Read-only lookup: list the caller's current and future appointments booked through this system. "
                    "Call when the caller wants to check, cancel, or reschedule an existing appointment and required phone context is available. "
                    "This lookup may take noticeable time; say a short preamble in the same turn before calling. "
                    "Match is by phone only (contact_name is not used for filtering). "
                    "Pass booking_hint with any time, summary, or other appointment facts the caller already gave so the tool can rank likely matches "
                    "and help you clarify which exact booking they mean before edit_booking or delete_booking when more than one booking is present. "
                    "Confirm the callback phone digit by digit when it is not already confirmed from caller context.\n\n"
                    "Preamble sample phrases:\n"
                    "- I'll look up your appointment details.\n"
                    "- I'll pull up your bookings now.\n"
                    "- I'll check what appointments you have on file."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "contact_phone": {"type": "string", "description": "Caller's phone. Omit to use the number from context (caller_phone). If the caller gives a different number verbally, confirm it digit by digit before calling."},
                        "contact_name": {"type": "string", "description": "Caller's name (optional; not used for filtering; pass for consistency with edit/delete)."},
                        "booking_hint": {"type": "string", "description": "Optional short factual hint from the caller such as requested time, what the appointment was for, or other identifying details."}
                    },
                    "required": []
                }
            })
            tools.append({
                "type": "function",
                "name": "delete_booking",
                "description": (
                    "Cancel the caller's appointment. Use the event_id from list_my_bookings. Ownership is verified by phone number (name is not used as an ownership gate because AI transcription can produce variants). "
                    "Only call this after the caller has confirmed the exact booking; if multiple bookings remain unresolved, do not use this tool and offer team follow-up instead. "
                    "This write action may take noticeable time; a short preamble in the same turn is appropriate after confirmation. "
                    "Do not say the appointment was cancelled until this tool succeeds. "
                    "If they want to book a new time after cancelling, call get_availability again—do not reuse an old slot list from the conversation.\n\n"
                    "Preamble sample phrases:\n"
                    "- I'll cancel that appointment now.\n"
                    "- I'll remove that booking for you.\n"
                    "- I'll take care of cancelling that now."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "event_id": {"type": "string", "description": "Event id from list_my_bookings for the appointment to cancel."},
                        "contact_phone": {"type": "string", "description": "Caller's phone. Omit to use the number from context (caller_phone). If the caller gives a different number verbally, confirm it digit by digit before calling."},
                        "contact_name": {"type": "string", "description": "Caller's name. Use with contact_phone to verify ownership."}
                    },
                    "required": ["event_id"]
                }
            })
            tools.append({
                "type": "function",
                "name": "edit_booking",
                "description": (
                    "Reschedule the caller's existing appointment to a new slot. Use event_id from list_my_bookings and new_slot_start_iso from get_availability. Ownership is verified by phone number (name is not used as an ownership gate because AI transcription can produce variants). "
                    "Only call this after the caller has confirmed the exact booking; if multiple bookings remain unresolved, do not use this tool and offer team follow-up instead. "
                    "This write action may take noticeable time; a short preamble in the same turn is appropriate after confirmation. "
                    "Do not say the appointment was rescheduled until this tool succeeds. "
                    "After success, if they want different times again, call get_availability again rather than reusing an earlier list.\n\n"
                    "Preamble sample phrases:\n"
                    "- I'll reschedule that appointment now.\n"
                    "- I'll move that booking to the new time.\n"
                    "- I'll update that appointment for you."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "event_id": {"type": "string", "description": "Event id from list_my_bookings for the appointment to reschedule."},
                        "new_slot_start_iso": {"type": "string", "description": "New start time (exact ISO value from get_availability). Do not invent or paraphrase the time."},
                        "contact_phone": {"type": "string", "description": "Caller's phone. Omit to use the number from context (caller_phone). If the caller gives a different number verbally, confirm it digit by digit before calling."},
                        "contact_name": {"type": "string", "description": "Caller's name. Use with contact_phone to verify ownership."}
                    },
                    "required": ["event_id", "new_slot_start_iso"]
                }
            })
        if Config.is_human_transfer_enabled():
            tools.append({
                "type": "function",
                "name": "request_human_handoff",
                "description": (
                    "Transfer the caller to a human agent. Call only when the caller explicitly asks to speak to a person (or escalation applies), "
                    "and only after you have at least their name and a brief reason. Do not transfer without minimal context. "
                    "Transfer may take noticeable time; say a short preamble in the same turn before calling. "
                    "Do not say the transfer is complete until this tool succeeds.\n\n"
                    "Preamble sample phrases:\n"
                    "- I'll connect you with someone now.\n"
                    "- I'll transfer you to a team member now.\n"
                    "- I'll get you to a person who can help."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "reason": {"type": "string", "description": "Brief reason for handoff (e.g. caller asked for a person, escalation)."},
                        "contact_name": {"type": "string", "description": "Caller's name if known."},
                        "contact_phone": {"type": "string", "description": "Callback number. Use actual number from context (caller_phone) if omitted."},
                        "issue_summary": {"type": "string", "description": "Brief issue or reason for call (for agent context)."},
                        "priority": {"type": "string", "description": "Urgency: emergency, same_day, routine (or high/normal)."},
                        "call_summary": {"type": "string", "description": "Short summary of the call for the agent."}
                    },
                    "required": ["reason"]
                }
            })
        if external_tool_registry is not None:
            try:
                load_mcp_tools(external_tool_registry)
                tools.extend(external_tool_registry.schemas())
            except Exception as e:
                Log.error(f"External tool registry load failed: {e}")
        return tools

    @staticmethod
    def create_session_update(
        caller_phone_number: Optional[str] = None,
        system_message_override: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Create a session update message for OpenAI Realtime API.
        - Model: from Config.OPENAI_REALTIME_MODEL (must match WS URL model).
        - Input/output: audio/pcmu (Twilio media stream format).
        - Voice: from Config.VOICE (e.g. marin, cedar); also set in WS URL at connect.
        - Caller number is not set here (API requires session.prompt.id for prompt.variables).
          It is passed via the initial conversation item (create_initial_conversation_item).
        - system_message_override: when set (e.g. outbound calls), uses this instead of Config.SYSTEM_MESSAGE.
        Tools list is built from config (webhook, booking).
        """
        turn_detection: Dict[str, Any]
        if Config.VAD_MODE == "semantic_vad":
            turn_detection = {
                "type": "semantic_vad",
                "eagerness": Config.VAD_EAGERNESS,
                "create_response": True,
                "interrupt_response": True,
            }
        else:
            turn_detection = {
                "type": "server_vad",
                "threshold": Config.VAD_THRESHOLD,
                "silence_duration_ms": Config.VAD_SILENCE_DURATION_MS,
                "prefix_padding_ms": Config.VAD_PREFIX_PADDING_MS,
            }
        audio_input: Dict[str, Any] = {
            "format": {"type": "audio/pcmu"},
            "turn_detection": turn_detection,
        }
        rt_tx_model = (getattr(Config, "REALTIME_INPUT_TRANSCRIPTION_MODEL", None) or "").strip()
        if rt_tx_model:
            tx: Dict[str, Any] = {"model": rt_tx_model}
            lang = getattr(Config, "REALTIME_INPUT_TRANSCRIPTION_LANGUAGE", None)
            if isinstance(lang, str) and lang.strip():
                tx["language"] = lang.strip()
            audio_input["transcription"] = tx
        instructions = system_message_override or Config.SYSTEM_MESSAGE
        runtime_policy_hint = _booking_runtime_policy_hint()
        if runtime_policy_hint:
            instructions = f"{instructions}\n\n{runtime_policy_hint}"
        session_payload: Dict[str, Any] = {
            "type": "realtime",
            "model": Config.OPENAI_REALTIME_MODEL,
            "output_modalities": ["audio"],
            "audio": {
                "input": audio_input,
                "output": {
                    "format": {"type": "audio/pcmu"},
                    "voice": Config.VOICE,
                }
            },
            "instructions": instructions,
            "tools": OpenAISessionManager._realtime_tools()
        }
        if (Config.OPENAI_REALTIME_MODEL or "").strip() == "gpt-realtime-2":
            session_payload["reasoning"] = {
                "effort": (getattr(Config, "REALTIME_REASONING_EFFORT", "low") or "low").strip().lower()
            }
        # Do not add session.prompt.variables here: the Realtime API requires session.prompt.id
        # when using prompt (stored prompts). Caller number is passed via the initial conversation
        # item (create_initial_conversation_item) so the model still gets it.
        return {"type": "session.update", "session": session_payload}
    
    @staticmethod
    def create_initial_conversation_item(
        caller_phone_number: Optional[str] = None,
        is_outbound: bool = False,
    ) -> Dict[str, Any]:
        """
        Create an initial conversation item for AI-first interactions.

        Inbound: uses the configured starter greeting so the voice agent
        speaks the brand welcome. Outbound: uses a minimal "begin now" instruction so
        the model follows its session-level outbound campaign instructions instead of
        the inbound greeting.
        """
        if is_outbound:
            instruction = (
                "The call has been answered. Begin the conversation now following "
                "your session instructions. Greet the person and proceed."
            )
            if caller_phone_number:
                instruction += (
                    f"\n\nContext: The contact's (person you are calling) phone number is {caller_phone_number}. "
                    "Use this for save_call_record contact_phone, book_appointment contact_phone, and similar tools—do not use the literal phrase 'caller_phone'."
                )
        else:
            greeting = get_greeting_instruction(Config.COMPANY_NAME)
            name = get_agent_name()
            as_who = f"as {name}, the voice agent" if name else "as the voice agent"
            instruction = (
                f"Greet the caller with the following (deliver it naturally and warmly, {as_who}): {greeting} "
                "Say only that greeting in the first response, then wait for the caller. Do not add a service list or extra intake question beyond the configured greeting."
            )
            if caller_phone_number:
                instruction += f"\n\nContext: The caller's phone number is {caller_phone_number}. You may use this when confirming contact details, in booking (book_appointment contact_phone), or when saving a call record. Do not read the number aloud unless confirming; use it when relevant."
        return {
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": instruction}
                ]
            }
        }
    
    @staticmethod
    def create_response_trigger() -> Dict[str, Any]:
        """
        Create a response trigger message.
        
        Returns:
            Dictionary to trigger OpenAI response generation
        """
        return {"type": "response.create"}


class OpenAIConversationManager:
    """
    Manages conversation flow and interruption logic for OpenAI sessions.
    
    - Creates truncation events to interrupt/cut off ongoing AI responses.
    - Determines when interruptions should be processed based on marks and timing.
    - Calculates elapsed time for precise truncation.
    
    Used by the main service to support real-time, interactive voice experiences.
    """
    
    @staticmethod
    def create_truncate_event(item_id: str, audio_end_ms: int) -> Dict[str, Any]:
        """
        Create a conversation item truncation event.
        
        Args:
            item_id: ID of the item to truncate
            audio_end_ms: Timestamp where to truncate the audio
            
        Returns:
            Dictionary containing truncation command
        """
        return {
            "type": "conversation.item.truncate",
            "item_id": item_id,
            "content_index": 0,
            "audio_end_ms": audio_end_ms
        }
    
    @staticmethod
    def should_handle_interruption(
        last_assistant_item: Optional[str],
        mark_queue: list,
        response_start_timestamp: Optional[int]
    ) -> bool:
        """
        Determine if an interruption should be processed.
        
        Args:
            last_assistant_item: ID of the last assistant response
            mark_queue: Queue of pending marks
            response_start_timestamp: When the current response started
            
        Returns:
            True if interruption should be handled
        """
        return (last_assistant_item is not None and 
                len(mark_queue) > 0 and 
                response_start_timestamp is not None)
    
    @staticmethod
    def calculate_truncation_time(
        current_timestamp: int,
        response_start_timestamp: int
    ) -> int:
        """
        Calculate the elapsed time for audio truncation.
        
        Args:
            current_timestamp: Current media timestamp
            response_start_timestamp: When the response started
            
        Returns:
            Elapsed time in milliseconds
        """
        return current_timestamp - response_start_timestamp


class OpenAIService:
    """
    Main service layer for all OpenAI Realtime API operations in the application.
    
    - Composes the event handler, session manager, and conversation manager.
    - Provides high-level methods to initialize sessions, send greetings, process/log events, extract audio, and handle interruptions.
    
    This is the primary interface for the rest of the application to interact with OpenAI, abstracting away lower-level event and session management details.
    """
    
    def __init__(self):
        self.session_manager = OpenAISessionManager()
        self.conversation_manager = OpenAIConversationManager()
        self.event_handler = OpenAIEventHandler()
        self._pending_tool_calls: Dict[str, Dict[str, Any]] = {}
        self._handled_tool_call_ids: set[str] = set()
        self._handled_tool_call_order: list[str] = []
        self._handled_tool_call_max: int = 512
        self._pending_goodbye: bool = False
        self._goodbye_audio_heard: bool = False
        self._goodbye_item_id: Optional[str] = None
        self._goodbye_watchdog: Optional[asyncio.Task] = None
        self._suppress_assistant_audio: bool = False

    def suppress_assistant_audio(self) -> None:
        """Block assistant audio deltas (e.g. filler before wait_for_user)."""
        self._suppress_assistant_audio = True

    def clear_assistant_audio_suppression(self) -> None:
        self._suppress_assistant_audio = False

    def should_suppress_assistant_audio(self) -> bool:
        return self._suppress_assistant_audio

    @staticmethod
    def _is_wait_for_user_tool_name(name: Any) -> bool:
        return isinstance(name, str) and name.strip() == "wait_for_user"

    @staticmethod
    def _normalize_tool_call_id(call_id: Optional[str]) -> Optional[str]:
        """Return a reliable tool call id for dedupe, or None when unavailable."""
        if not isinstance(call_id, str):
            return None
        normalized = call_id.strip()
        if not normalized or normalized == "default":
            return None
        return normalized

    def _mark_tool_call_handled(self, call_id: Optional[str]) -> None:
        """Track recently handled tool call ids to suppress duplicate executions."""
        normalized = self._normalize_tool_call_id(call_id)
        if not normalized:
            return
        if normalized in self._handled_tool_call_ids:
            return
        self._handled_tool_call_ids.add(normalized)
        self._handled_tool_call_order.append(normalized)
        if len(self._handled_tool_call_order) > self._handled_tool_call_max:
            evicted = self._handled_tool_call_order.pop(0)
            self._handled_tool_call_ids.discard(evicted)

    @staticmethod
    def _as_text(value: Any) -> str:
        """Normalize arbitrary value to a trimmed string (empty when missing)."""
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        return str(value).strip()

    @staticmethod
    def _is_iso_datetime(value: str) -> bool:
        """True when value parses as ISO datetime (accepts trailing Z)."""
        text = (value or "").strip()
        if not text:
            return False
        try:
            datetime.fromisoformat(text.replace("Z", "+00:00"))
            return True
        except ValueError:
            return False

    @staticmethod
    def _is_yyyy_mm_dd(value: str) -> bool:
        """True when value is a YYYY-MM-DD date."""
        text = (value or "").strip()
        if not text:
            return False
        try:
            datetime.strptime(text, "%Y-%m-%d")
            return True
        except ValueError:
            return False

    @staticmethod
    def _as_bool(value: Any) -> bool:
        """Lenient bool parser for tool args."""
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        text = str(value or "").strip().lower()
        return text in ("1", "true", "yes", "y")

    @staticmethod
    def _slot_iso_to_friendly_display(slot_start_iso: str) -> str:
        """Best-effort friendly local display for caller confirmation."""
        raw = (slot_start_iso or "").strip()
        if not raw:
            return "the selected time"
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                return raw
            try:
                from zoneinfo import ZoneInfo

                tz = ZoneInfo((Config.TIMEZONE or "America/Los_Angeles").strip() or "America/Los_Angeles")
                local_dt = dt.astimezone(tz)
                return local_dt.strftime("%A, %B %d at %I:%M %p")
            except Exception:
                return dt.strftime("%A, %B %d at %I:%M %p")
        except Exception:
            return raw

    def _normalize_and_validate_tool_args(
        self,
        tool_name: str,
        args: Dict[str, Any],
        connection_manager,
    ) -> tuple[bool, Dict[str, Any], Optional[str]]:
        """
        Normalize and validate tool arguments before side effects.
        Returns (ok, normalized_args, error_message_or_none).
        """
        normalized = dict(args or {})
        caller_phone = self._as_text(getattr(connection_manager.state, "caller_phone_number", None))

        def _phone_with_fallback(raw: Any) -> str:
            p = self._as_text(raw)
            override_reason = _caller_phone_override_reason(p, caller_phone, normalized)
            if override_reason:
                if p and caller_phone and p != caller_phone:
                    Log.event("Tool phone corrected from caller context", {
                        "tool_name": tool_name,
                        "model_phone": p,
                        "caller_phone": caller_phone,
                        "reason": override_reason,
                    })
                return caller_phone
            return p

        if tool_name == "end_call":
            normalized["reason"] = self._as_text(normalized.get("reason"))
            return True, normalized, None

        if tool_name == "request_human_handoff":
            normalized["reason"] = self._as_text(normalized.get("reason"))
            normalized["contact_name"] = self._as_text(normalized.get("contact_name"))
            normalized["contact_phone"] = _phone_with_fallback(normalized.get("contact_phone"))
            normalized["contact_email"] = self._as_text(normalized.get("contact_email"))
            normalized["issue_summary"] = self._as_text(normalized.get("issue_summary"))
            normalized["priority"] = self._as_text(normalized.get("priority"))
            normalized["call_summary"] = self._as_text(normalized.get("call_summary"))
            normalized["preferred_callback_time"] = self._as_text(normalized.get("preferred_callback_time")) or None
            normalized["service_address"] = self._as_text(normalized.get("service_address")) or None
            if not normalized["reason"]:
                return False, normalized, "Missing required field for request_human_handoff: reason."
            return True, normalized, None

        if tool_name in {"save_call_record", "submit_lead"}:
            tool_label = "save_call_record" if tool_name == "save_call_record" else "submit_lead"
            normalized["contact_name"] = self._as_text(normalized.get("contact_name"))
            normalized["contact_phone"] = _phone_with_fallback(normalized.get("contact_phone"))
            normalized["contact_email"] = self._as_text(normalized.get("contact_email"))
            normalized["issue_summary"] = self._as_text(normalized.get("issue_summary"))
            normalized["priority"] = self._as_text(normalized.get("priority"))
            normalized["call_summary"] = self._as_text(normalized.get("call_summary"))
            normalized["preferred_callback_time"] = self._as_text(normalized.get("preferred_callback_time")) or None
            normalized["service_address"] = self._as_text(normalized.get("service_address")) or None
            confirmed_slot = normalized.get("confirmed_slot")
            if confirmed_slot not in (None, "") and not isinstance(confirmed_slot, dict):
                return False, normalized, f"Invalid confirmed_slot for {tool_label}; expected an object."
            required = ["contact_name", "contact_phone", "issue_summary", "priority", "call_summary"]
            missing = [f for f in required if not normalized.get(f)]
            if missing:
                return False, normalized, f"Missing required field(s) for {tool_label}: {', '.join(missing)}."
            return True, normalized, None

        if tool_name == "get_availability":
            raw_days = normalized.get("days_ahead", 7)
            try:
                days = int(raw_days)
            except (TypeError, ValueError):
                return False, normalized, "Invalid days_ahead for get_availability; expected an integer."
            if days < 1 or days > 31:
                return False, normalized, "Invalid days_ahead for get_availability; expected 1-31."
            for_date = self._as_text(normalized.get("for_date")) or None
            if for_date and not self._is_yyyy_mm_dd(for_date):
                return False, normalized, "Invalid for_date for get_availability; expected YYYY-MM-DD."
            normalized["days_ahead"] = days
            normalized["for_date"] = for_date
            return True, normalized, None

        if tool_name == "book_appointment":
            normalized["slot_start_iso"] = self._as_text(normalized.get("slot_start_iso"))
            normalized["contact_name"] = self._as_text(normalized.get("contact_name"))
            normalized["contact_phone"] = _phone_with_fallback(normalized.get("contact_phone"))
            normalized["contact_email"] = self._as_text(normalized.get("contact_email")) or None
            normalized["summary"] = self._as_text(normalized.get("summary")) or None
            normalized["confirm_exact_slot"] = self._as_bool(normalized.get("confirm_exact_slot"))
            missing = [f for f in ("slot_start_iso", "contact_name", "contact_phone") if not normalized.get(f)]
            if missing:
                return False, normalized, f"Missing required field(s) for book_appointment: {', '.join(missing)}."
            if not self._is_iso_datetime(normalized["slot_start_iso"]):
                return False, normalized, "Invalid slot_start_iso for book_appointment; expected ISO datetime."
            return True, normalized, None

        if tool_name == "list_my_bookings":
            normalized["contact_phone"] = _phone_with_fallback(normalized.get("contact_phone"))
            normalized["contact_name"] = self._as_text(normalized.get("contact_name")) or None
            normalized["booking_hint"] = self._as_text(normalized.get("booking_hint")) or None
            return True, normalized, None

        if tool_name == "delete_booking":
            normalized["event_id"] = self._as_text(normalized.get("event_id"))
            normalized["contact_phone"] = _phone_with_fallback(normalized.get("contact_phone"))
            normalized["contact_name"] = self._as_text(normalized.get("contact_name")) or None
            if not normalized["event_id"]:
                return False, normalized, "Missing required field for delete_booking: event_id."
            return True, normalized, None

        if tool_name == "edit_booking":
            normalized["event_id"] = self._as_text(normalized.get("event_id"))
            normalized["new_slot_start_iso"] = self._as_text(normalized.get("new_slot_start_iso"))
            normalized["contact_phone"] = _phone_with_fallback(normalized.get("contact_phone"))
            normalized["contact_name"] = self._as_text(normalized.get("contact_name")) or None
            missing = [f for f in ("event_id", "new_slot_start_iso") if not normalized.get(f)]
            if missing:
                return False, normalized, f"Missing required field(s) for edit_booking: {', '.join(missing)}."
            if not self._is_iso_datetime(normalized["new_slot_start_iso"]):
                return False, normalized, "Invalid new_slot_start_iso for edit_booking; expected ISO datetime."
            return True, normalized, None

        return True, normalized, None
    
    async def initialize_session(
        self,
        connection_manager,
        system_message_override: str | None = None,
    ) -> None:
        """
        Initialize OpenAI session with proper configuration.
        Passes caller phone number into session prompt.variables when available (for confirming with the customer).
        
        Args:
            connection_manager: WebSocket connection manager
            system_message_override: When set (outbound calls), replaces Config.SYSTEM_MESSAGE for this session.
        """
        caller_phone = getattr(connection_manager.state, "caller_phone_number", None)
        if caller_phone:
            Log.event("caller_phone in OpenAI Realtime prompt (source: Twilio From)", {
                "caller_phone": caller_phone,
                "injected_into": ["session.prompt.variables.caller_phone", "initial conversation item text"],
            })
        session_update = self.session_manager.create_session_update(
            caller_phone_number=caller_phone,
            system_message_override=system_message_override,
        )
        tools = session_update.get("session", {}).get("tools", [])
        transfer_enabled = Config.is_human_transfer_enabled()
        handoff_in_tools = any(
            isinstance(t, dict) and t.get("name") == "request_human_handoff" for t in tools
        )
        Log.event("Human transfer", {
            "enabled": transfer_enabled,
            "request_human_handoff_in_tools": handoff_in_tools,
        })
        session_log_summary = {
            "type": session_update.get("type"),
            "session": {
                "type": session_update.get("session", {}).get("type"),
                "model": session_update.get("session", {}).get("model"),
            },
        }
        Log.json("Sending session update", session_log_summary)
        await connection_manager.send_to_openai(session_update)

    def prewarm_availability_cache(self) -> None:
        """
        Pre-warm get_availability cache in the background (default week view).
        Call when the stream starts so the first booking request can be served from cache.
        Non-blocking: schedules a task and returns immediately.
        """
        if not is_booking_enabled():
            return

        async def _run_prewarm() -> None:
            try:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(
                    None,
                    lambda: booking_get_availability(days_ahead=7, for_date=None),
                )
                Log.info("Availability cache pre-warmed (default week)")
            except Exception as e:
                Log.error(f"Availability pre-warm failed: {e}")

        asyncio.create_task(_run_prewarm())

    async def send_caller_phone_session_update(self, connection_manager) -> None:
        """
        When caller number was set after initial session (e.g. from CallSid cache on stream start),
        we do NOT send a second session.update with prompt.variables: the Realtime API requires
        session.prompt.id when using prompt.variables (stored prompts). We don't use stored prompts.
        The caller number is already passed in the initial conversation item (send_initial_greeting
        runs right after and includes it in the greeting instruction), so the model still gets it.
        This method now only logs for traceability; no session.update is sent.
        """
        caller_phone = getattr(connection_manager.state, "caller_phone_number", None)
        if not caller_phone:
            return
        Log.event("Caller number from cache — passed via initial greeting (no session.prompt; API requires prompt.id)", {"caller_phone": caller_phone})

    async def send_initial_greeting(self, connection_manager, *, is_outbound: bool = False) -> None:
        """
        Send initial conversation item to make AI greet first.

        For inbound: includes caller phone number and the configured greeting.
        For outbound: sends a minimal "begin now" item so the model follows
        its session-level campaign instructions for the first response.

        Args:
            connection_manager: WebSocket connection manager
            is_outbound: True when this is an outbound campaign call
        """
        caller_phone_number = getattr(connection_manager.state, "caller_phone_number", None)
        if is_outbound:
            Log.event("Outbound greeting: using session instructions (no inbound greeting injected)", {})
        elif caller_phone_number:
            Log.event("Incoming caller number (traceability)", {
                "incoming_caller_number": caller_phone_number,
                "context": "passed to session for confirmation/lead and booking",
            })
        else:
            Log.event("No caller number passed to OpenAI — model has no caller_phone; must ask or will have no number", {})
        initial_item = self.session_manager.create_initial_conversation_item(
            caller_phone_number, is_outbound=is_outbound,
        )
        response_trigger = self.session_manager.create_response_trigger()

        await connection_manager.send_to_openai(initial_item)
        await connection_manager.send_to_openai(response_trigger)
    
    def _condensed_response_done(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """
        Build a condensed payload for response.done: only output items with
        type/role/content (message + transcript) or type/name/call_id/arguments (function_call).
        """
        resp = event.get('response') or {}
        output = resp.get('output') or []
        condensed = []
        for item in output:
            if not isinstance(item, dict):
                continue
            if item.get('type') == 'message':
                content = []
                for c in (item.get('content') or []):
                    if isinstance(c, dict) and c.get('type') == 'output_audio':
                        content.append({"type": "output_audio", "transcript": c.get('transcript', '')})
                condensed.append({
                    "type": "message",
                    "role": item.get('role', 'assistant'),
                    "content": content,
                })
            elif item.get('type') == 'function_call':
                condensed.append({
                    "type": "function_call",
                    "name": item.get('name'),
                    "call_id": item.get('call_id'),
                    "arguments": item.get('arguments', ''),
                })
        return {"output": condensed} if condensed else event

    def _response_done_log_label(self, payload: Dict[str, Any]) -> str:
        """Build a one-line label for response.done from condensed payload, e.g. (message) or (function_call: save_call_record)."""
        output = payload.get("output") or []
        if not output:
            return "response.done"
        parts = []
        for item in output:
            if isinstance(item, dict):
                if item.get("type") == "message":
                    parts.append("message")
                elif item.get("type") == "function_call":
                    name = item.get("name") or "?"
                    parts.append(f"function_call: {name}")
        label = ", ".join(parts) if parts else "response.done"
        return f"response.done ({label})"

    def process_event_for_logging(self, event: Dict[str, Any]) -> None:
        """
        Process OpenAI event for logging if needed.
        For response.done, logs a condensed payload (message transcript or function_call name/arguments only).
        """
        etype = event.get('type', '')
        # Caller speech text (Realtime input transcription). Always log when enabled server-side;
        # not gated on LOG_EVENT_TYPES to avoid noise from raw delta streams.
        if etype == 'conversation.item.input_audio_transcription.completed':
            transcript = (event.get('transcript') or '').strip()
            payload = {
                "transcript": transcript,
                "item_id": event.get('item_id'),
                "content_index": event.get('content_index'),
            }
            if transcript:
                Log.event('Caller said', payload)
            else:
                Log.event('Caller transcript (empty)', payload)
            return
        if etype == 'conversation.item.input_audio_transcription.failed':
            err = event.get('error') if isinstance(event.get('error'), dict) else event.get('error')
            Log.event('Caller transcript failed', {
                'item_id': event.get('item_id'),
                'error': err or event,
            })
            return
        if not self.event_handler.should_log_event(etype):
            return
        if etype == 'response.done':
            payload = self._condensed_response_done(event)
            label = self._response_done_log_label(payload)
            Log.event(f"Received event: {label}", payload)
        elif etype == 'session.created':
            Log.event("Received event: session.created", {
                "message": "Default session from OpenAI when the Realtime connection is established (required for Realtime models). We then send session.update to apply our instructions, tools, and voice.",
            })
        elif etype == 'session.updated':
            Log.event(f"Received event: {etype}", event)
            # Confirm applied voice (session.created shows server default; session.updated is our config)
            audio = (event.get('session') or {}).get('audio') or {}
            applied_voice = (audio.get('output') or {}).get('voice')
            if applied_voice:
                Log.info(f"Session voice applied: {applied_voice}")
        elif etype == 'error':
            err = event.get('error') or {}
            code = err.get('code') or ''
            msg = err.get('message') or ''
            if code == 'rate_limit_exceeded':
                # TPM = tokens per minute; limit is per-org, not app-configurable. See docs/errors-and-debugging/RATE_LIMIT_TPM.md
                Log.event("Received event: error (rate_limit_exceeded)", {"code": code, "message": msg, "hint": "Raise TPM at https://platform.openai.com/account/rate-limits"})
            else:
                Log.event(f"Received event: {etype}", event)
        else:
            Log.event(f"Received event: {etype}", event)

    def is_tool_call(self, event: Dict[str, Any]) -> bool:
        """Return True if the event is a tool call from the model."""
        etype = event.get('type')
        if etype in ('response.function_call.arguments.delta', 'response.function_call.completed'):
            return True
        # Also detect tool/function calls embedded in response.done payloads
        if etype == 'response.done':
            resp = event.get('response') or {}
            output = resp.get('output') or []
            for item in output:
                if isinstance(item, dict) and item.get('type') == 'function_call':
                    return True
        return False

    def accumulate_tool_call(self, event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Accumulate streamed tool call arguments until completion.
        Returns the completed call payload when finished.
        """
        etype = event.get('type')
        if etype == 'response.function_call.arguments.delta':
            if self._is_wait_for_user_tool_name(event.get('name')):
                self.suppress_assistant_audio()
            call_id = event.get('call_id') or event.get('id') or 'default'
            delta = event.get('delta', '')
            buf = self._pending_tool_calls.setdefault(call_id, {"args": "", "name": event.get('name')})
            buf["args"] += delta
            return None
        if etype == 'response.function_call.completed':
            if self._is_wait_for_user_tool_name(event.get('name')):
                self.suppress_assistant_audio()
            call_id = event.get('call_id') or event.get('id') or 'default'
            payload = self._pending_tool_calls.pop(call_id, None)
            if payload is None:
                return None
            try:
                args = json.loads(payload["args"]) if payload["args"] else {}
            except Exception:
                args = {"_raw": payload["args"]}
            return {"name": payload.get('name') or event.get('name'), "arguments": args, "call_id": call_id}
        # Handle non-streamed function calls embedded in response.done
        if etype == 'response.done':
            resp = event.get('response') or {}
            output = resp.get('output') or []
            for item in output:
                if isinstance(item, dict) and item.get('type') == 'function_call':
                    name = item.get('name')
                    raw_args = item.get('arguments')
                    args: Dict[str, Any]
                    try:
                        args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
                    except Exception:
                        args = {"_raw": raw_args}
                    # API expects call_id (e.g. call_XXX), not the item id
                    call_id = item.get('call_id') or item.get('id')
                    return {"name": name, "arguments": args, "call_id": call_id}
        return None

    @staticmethod
    def _format_tool_failure_output(message: str, *, next_step: str | None = None) -> str:
        """Return structured tool failure output the model can recover from cleanly."""
        payload: Dict[str, Any] = {"success": False, "message": message}
        if next_step:
            payload["next_step"] = next_step
        return json.dumps(payload)

    async def _send_tool_result(
        self,
        connection_manager,
        call_id: Optional[str],
        output: str,
        *,
        trigger_response: bool = True,
    ) -> None:
        """Send a function_call_output item and optionally trigger a new response."""
        item = {"type": "function_call_output", "output": output}
        if call_id:
            item["call_id"] = call_id
        try:
            await connection_manager.send_to_openai({
                "type": "conversation.item.create",
                "item": item
            })
            if trigger_response:
                await connection_manager.send_to_openai({"type": "response.create"})
        except Exception as e:
            Log.error(f"Failed to send tool result: {e}")

    async def maybe_handle_tool_call(self, connection_manager, tool_call: Dict[str, Any]) -> bool:
        """
        Handle supported tool calls. Returns True if a tool was handled.
        Supports: wait_for_user, end_call, save_call_record, get_availability, book_appointment
        """
        if not tool_call:
            return False
        name = tool_call.get('name')
        args = tool_call.get('arguments') or {}
        if not isinstance(args, dict):
            args = {}
        call_id = tool_call.get('call_id')
        dedupe_call_id = self._normalize_tool_call_id(call_id)
        known_tools = {
            "wait_for_user",
            "end_call",
            "request_human_handoff",
            "save_call_record",
            "submit_lead",
            "get_availability",
            "list_my_bookings",
            "delete_booking",
            "edit_booking",
            "book_appointment",
        }
        if dedupe_call_id and name in known_tools and dedupe_call_id in self._handled_tool_call_ids:
            Log.info(f"Ignoring duplicate tool call: {name} ({dedupe_call_id})")
            return True
        if dedupe_call_id and name in known_tools:
            self._mark_tool_call_handled(dedupe_call_id)
        if name in known_tools:
            ok, args, validation_error = self._normalize_and_validate_tool_args(
                name, args, connection_manager
            )
            if not ok:
                Log.event("Tool argument validation failed", {
                    "tool_name": name,
                    "call_id": dedupe_call_id or call_id,
                    "error": validation_error,
                    "arg_keys": sorted(list(args.keys())),
                })
                await self._send_tool_result(
                    connection_manager,
                    call_id,
                    self._format_tool_failure_output(
                        validation_error or f"Invalid arguments for {name}.",
                        next_step="Ask for any missing or corrected details before retrying the tool.",
                    ),
                )
                return True

        if name == "wait_for_user":
            self.suppress_assistant_audio()
            state = connection_manager.state
            count = int(getattr(state, "wait_for_user_count", 0) or 0) + 1
            state.wait_for_user_count = count
            Log.event("wait_for_user", {
                "call_sid": getattr(state, "call_sid", None),
                "count": count,
            })
            await self._send_tool_result(
                connection_manager,
                call_id,
                "OK. Stay silent and keep listening. Do not produce a spoken reply.",
                trigger_response=False,
            )
            return True

        if name == 'end_call':
            reason = args.get('reason')
            state = connection_manager.state
            farewell = get_farewell_instruction(
                Config.COMPANY_NAME,
                reason,
                call_record_saved=getattr(state, "call_record_saved", False),
                appointment_booked=getattr(state, "appointment_booked", False),
                confirmed_slot_display=getattr(state, "confirmed_slot_display", None),
                priority=getattr(state, "priority", None),
            )
            if self._pending_goodbye:
                Log.info("End-call already pending; ignoring duplicate request")
                return False
            Log.info("Queueing farewell response before hangup")
            await self._send_goodbye_response(connection_manager, farewell)
            self._pending_goodbye = True
            self._goodbye_audio_heard = False
            self._goodbye_item_id = None
            self._start_goodbye_watchdog(connection_manager)
            return True

        if name == 'request_human_handoff':
            call_sid = getattr(connection_manager.state, "call_sid", None)
            if not call_sid:
                await self._send_tool_result(
                    connection_manager, call_id,
                    "Transfer not available; no call context."
                )
                return True
            transfer_url = Config.get_transfer_url()
            if not transfer_url:
                await self._send_tool_result(
                    connection_manager, call_id,
                    "Transfer not available."
                )
                return True
            if has_call_record_backend_configured():
                from services.call_records_service import (
                    build_call_record_payload,
                    save_call_record_async,
                )
                contact_phone = (args.get("contact_phone") or "").strip()
                if not contact_phone or _is_placeholder_phone(contact_phone):
                    contact_phone = getattr(connection_manager.state, "caller_phone_number", None) or ""
                contact = {
                    "name": args.get("contact_name") or "",
                    "phone": contact_phone,
                    "email": args.get("contact_email") or "",
                }
                payload = build_call_record_payload(
                    contact=contact,
                    issue_summary=args.get("issue_summary") or "",
                    priority=args.get("priority") or "normal",
                    call_summary=args.get("call_summary") or "",
                    preferred_callback_time=args.get("preferred_callback_time"),
                    confirmed_slot=args.get("confirmed_slot"),
                    transcript=None,
                    call_sid=call_sid,
                    service_address=(args.get("service_address") or "").strip() or None,
                )
                asyncio.create_task(save_call_record_async(payload))
            from services.twilio_service import TwilioService
            await TwilioService.redirect_call_to_url_async(call_sid, transfer_url)
            connection_manager.mark_twilio_closed()
            await self._send_tool_result(
                connection_manager, call_id,
                "Transferring you now."
            )
            return True

        if name in {'save_call_record', 'submit_lead'}:
            from services.call_records_service import (
                build_call_record_payload,
                save_call_record_async,
                update_call_record_by_call_sid_async,
                call_record_payload_to_supabase_updates,
                update_existing_call_record_from_payload_async,
            )
            contact_phone = (args.get("contact_phone") or "").strip()
            if not contact_phone or _is_placeholder_phone(contact_phone):
                fallback = getattr(connection_manager.state, "caller_phone_number", None) or ""
                if fallback:
                    contact_phone = fallback
            contact = {
                "name": args.get("contact_name") or "",
                "phone": contact_phone,
                "email": args.get("contact_email") or "",
            }
            call_sid = getattr(connection_manager.state, "call_sid", None)
            payload = build_call_record_payload(
                contact=contact,
                issue_summary=args.get("issue_summary") or "",
                priority=args.get("priority") or "normal",
                call_summary=args.get("call_summary") or "",
                preferred_callback_time=args.get("preferred_callback_time"),
                confirmed_slot=args.get("confirmed_slot"),
                transcript=None,
                call_sid=call_sid,
                service_address=(args.get("service_address") or "").strip() or None,
            )
            already_submitted = getattr(connection_manager.state, "call_record_saved", getattr(connection_manager.state, "lead_submitted", False))
            resolved_call_record_id = getattr(connection_manager.state, "resolved_call_record_id", getattr(connection_manager.state, "resolved_business_lead_id", None))
            if already_submitted and call_sid:
                # Second save_call_record in same call: update the existing call record by call_sid when using Supabase.
                backend = (Config.CALL_RECORD_BACKEND or "webhook").strip().lower()
                if backend == "supabase":
                    updates = call_record_payload_to_supabase_updates(payload)
                    asyncio.create_task(update_call_record_by_call_sid_async(call_sid, updates))
                    await self._send_tool_result(
                        connection_manager, call_id,
                        "Call record updated with your preferences."
                    )
                else:
                    await self._send_tool_result(
                        connection_manager, call_id,
                        "Noted. Our team will follow up shortly."
                    )
                return True
            if resolved_call_record_id and call_sid and (Config.CALL_RECORD_BACKEND or "webhook").strip().lower() == "supabase":
                asyncio.create_task(update_existing_call_record_from_payload_async(resolved_call_record_id, payload))
                connection_manager.state.call_record_saved = True
                connection_manager.state.lead_submitted = True
                connection_manager.state.priority = (args.get("priority") or "").strip() or None
                await self._send_tool_result(
                    connection_manager, call_id,
                    "Call record updated with the latest call details."
                )
                return True
            asyncio.create_task(save_call_record_async(payload))
            connection_manager.state.call_record_saved = True
            connection_manager.state.lead_submitted = True
            connection_manager.state.priority = (args.get("priority") or "").strip() or None
            await self._send_tool_result(
                connection_manager, call_id,
                "Call record saved. Our team will follow up if needed."
            )
            return True

        if name == 'get_availability':
            days_ahead = args.get("days_ahead", 7)
            for_date = (args.get("for_date") or "").strip() or None
            # Run blocking Google Calendar call in thread so the event loop stays responsive
            result = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: booking_get_availability(days_ahead=days_ahead, for_date=for_date),
            )
            by_day = result.get("by_day") or {}
            slots_flat = result.get("slots_flat") or []
            closed_day = result.get("closed_day") if isinstance(result, dict) else None
            if isinstance(closed_day, dict):
                date_iso = self._as_text(closed_day.get("date")) or (for_date or "")
                weekday_display = self._as_text(closed_day.get("weekday_display")) or "That day"
                try:
                    date_display = datetime.strptime(date_iso, "%Y-%m-%d").strftime("%A, %B %d")
                except Exception:
                    date_display = date_iso or "the requested date"
                allowed_days = closed_day.get("allowed_days")
                closed_days = closed_day.get("closed_days")
                allowed_text = _format_weekday_tokens(allowed_days if isinstance(allowed_days, list) else None)
                closed_text = _format_weekday_tokens(closed_days if isinstance(closed_days, list) else None)
                msg = (
                    f"{weekday_display} ({date_display}) is closed for booking based on configured booking days. "
                )
                if allowed_text:
                    msg += f"Bookings are currently open on {allowed_text}. "
                if closed_text:
                    msg += f"Closed days are {closed_text}. "
                msg += "Ask the caller for another open day and call get_availability for that date."
                await self._send_tool_result(connection_manager, call_id, msg)
            elif not by_day and not slots_flat:
                await self._send_tool_result(
                    connection_manager, call_id,
                    "No availability could be loaded. Suggest the caller leave their details and we'll call to schedule."
                )
            else:
                lines = []
                for date_str, day_data in sorted(by_day.items()):
                    date_display = day_data.get("date_display", date_str)
                    parts = [f"{date_display}:"]
                    for bucket in ("morning", "afternoon", "evening"):
                        slot_list = day_data.get(bucket) or []
                        if slot_list:
                            times = [f"{s['display']} ({s['start']})" for s in slot_list]
                            parts.append(f"  {bucket.capitalize()}: " + "; ".join(times))
                    if len(parts) > 1:
                        lines.append("\n".join(parts))
                body = "\n\n".join(lines) if lines else "No slots in the requested range."
                if slots_flat:
                    first = slots_flat[0]
                    body = f"Earliest slot: {first.get('display', first.get('start', ''))} ({first.get('start', '')}) — suggest this first for emergencies.\n\n" + body
                msg = "Available slots by day and time (use the ISO start in parentheses for book_appointment):\n\n" + body
                await self._send_tool_result(connection_manager, call_id, msg)
            return True

        if name == 'list_my_bookings':
            list_phone = (args.get("contact_phone") or "").strip()
            if not list_phone or _is_placeholder_phone(list_phone):
                list_phone = getattr(connection_manager.state, "caller_phone_number", None) or ""
            list_name = (args.get("contact_name") or "").strip() or None
            booking_hint = (args.get("booking_hint") or "").strip() or None
            result = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: booking_list_my_bookings(contact_phone=list_phone, contact_name=list_name),
            )
            if isinstance(result, list) and result:
                ranked = _rank_booking_candidates(result, contact_name=list_name, booking_hint=booking_hint)
                top_score = int((ranked[0].get("_candidate_score") or 0)) if ranked else 0
                second_score = int((ranked[1].get("_candidate_score") or 0)) if len(ranked) > 1 else -1
                if len(ranked) == 1:
                    lines = ["Caller has 1 booking:"]
                elif top_score > 0 and top_score >= second_score + 2:
                    lines = [f"Caller has {len(ranked)} booking(s). Best candidate based on caller details:"]
                else:
                    lines = [
                        f"Caller has {len(ranked)} booking(s). "
                        + ("No strong match from caller details yet; " if top_score <= 0 else "Multiple possible matches remain; ")
                        + "ask a clarifying question before edit/delete."
                    ]
                for idx, b in enumerate(ranked):
                    display = b.get('display', b.get('start', ''))
                    summary = self._as_text(b.get("summary"))
                    booked_under = self._as_text(b.get("caller_name"))
                    visit_summary = self._as_text(b.get("visit_summary"))
                    service_type = self._as_text(b.get("service_type"))
                    match_reasons = [self._as_text(r) for r in (b.get("_candidate_reasons") or []) if self._as_text(r)]
                    details = []
                    if summary:
                        details.append(summary)
                    if booked_under:
                        details.append(f"booked under {booked_under}")
                    if visit_summary and visit_summary.lower() not in " ".join(details).lower():
                        details.append(f"details: {visit_summary}")
                    if service_type and service_type.lower() not in " ".join(details).lower():
                        details.append(f"service type: {service_type}")
                    label = "  BEST: " if len(ranked) > 1 and idx == 0 and top_score > 0 and top_score >= second_score + 2 else "  "
                    suffix = f" (event_id: {b.get('event_id', '')})"
                    if match_reasons:
                        suffix += "; match signals: " + "; ".join(match_reasons[:3])
                    if details:
                        lines.append(label + f"{display} — " + " — ".join(details) + suffix)
                    else:
                        lines.append(label + f"{display}" + suffix)
                msg = "\n".join(lines)
            elif isinstance(result, list):
                msg = "Caller has no upcoming appointments."
            else:
                msg = "Could not load appointments."
            await self._send_tool_result(connection_manager, call_id, msg)
            return True

        if name == 'delete_booking':
            del_phone = (args.get("contact_phone") or "").strip()
            if not del_phone or _is_placeholder_phone(del_phone):
                del_phone = getattr(connection_manager.state, "caller_phone_number", None) or ""
            del_name = (args.get("contact_name") or "").strip() or None
            result = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: booking_delete_booking(event_id=args.get("event_id") or "", contact_phone=del_phone, contact_name=del_name),
            )
            if isinstance(result, dict) and result.get("success"):
                call_sid = getattr(connection_manager.state, "call_sid", None)
                note = "Caller requested cancellation of appointment. Confirmed cancelled."
                if call_sid:
                    from services.call_records_service import sync_call_record_after_booking_action_async
                    task = asyncio.create_task(
                        sync_call_record_after_booking_action_async(
                            action="cancelled",
                            call_sid=call_sid,
                            event_id=args.get("event_id") or "",
                            contact_phone=del_phone,
                            confirmed_slot=None,
                            calendar_event_link=None,
                            note=note,
                        )
                    )
                    def _remember_resolved_lead(t: asyncio.Task) -> None:
                        try:
                            lead_id = t.result()
                        except Exception:
                            return
                        if lead_id:
                            connection_manager.state.resolved_call_record_id = lead_id
                            connection_manager.state.resolved_business_lead_id = lead_id
                    task.add_done_callback(_remember_resolved_lead)
            msg = result.get("message", "Done.") if isinstance(result, dict) else "Done."
            await self._send_tool_result(connection_manager, call_id, msg)
            return True

        if name == 'edit_booking':
            edit_phone = (args.get("contact_phone") or "").strip()
            if not edit_phone or _is_placeholder_phone(edit_phone):
                edit_phone = getattr(connection_manager.state, "caller_phone_number", None) or ""
            edit_name = (args.get("contact_name") or "").strip() or None
            result = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: booking_edit_booking(
                    event_id=args.get("event_id") or "",
                    new_slot_start_iso=args.get("new_slot_start_iso") or "",
                    contact_phone=edit_phone,
                    contact_name=edit_name,
                ),
            )
            if isinstance(result, dict) and result.get("success"):
                slot = result.get("confirmed_slot")
                connection_manager.state.appointment_booked = True
                connection_manager.state.confirmed_slot_display = (
                    (slot.get("display") if isinstance(slot, dict) else None) or result.get("message")
                )
                call_sid = getattr(connection_manager.state, "call_sid", None)
                if call_sid and result.get("confirmed_slot") is not None:
                    from services.call_records_service import sync_call_record_after_booking_action_async
                    display = (slot.get("display") if isinstance(slot, dict) else None) or result.get("message", "")
                    note = (
                        f"Caller requested reschedule. Appointment moved to {display}."
                        if display else "Caller requested reschedule. Appointment updated."
                    )
                    task = asyncio.create_task(
                        sync_call_record_after_booking_action_async(
                            action="rescheduled",
                            call_sid=call_sid,
                            event_id=args.get("event_id") or "",
                            contact_phone=edit_phone,
                            confirmed_slot=result["confirmed_slot"],
                            note=note,
                        )
                    )
                    def _remember_resolved_lead(t: asyncio.Task) -> None:
                        try:
                            lead_id = t.result()
                        except Exception:
                            return
                        if lead_id:
                            connection_manager.state.resolved_call_record_id = lead_id
                            connection_manager.state.resolved_business_lead_id = lead_id
                    task.add_done_callback(_remember_resolved_lead)
            msg = result.get("message", "Done.") if isinstance(result, dict) else "Done."
            await self._send_tool_result(connection_manager, call_id, msg)
            return True

        if name == 'book_appointment':
            slot_start_iso = args.get("slot_start_iso") or ""
            book_phone = (args.get("contact_phone") or "").strip()
            if not book_phone or _is_placeholder_phone(book_phone):
                book_phone = getattr(connection_manager.state, "caller_phone_number", None) or ""
            # Run blocking Google Calendar call in thread so the event loop stays responsive
            result = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: booking_book_appointment(
                    slot_start_iso=slot_start_iso,
                    contact_name=args.get("contact_name") or "",
                    contact_phone=book_phone,
                    contact_email=args.get("contact_email"),
                    summary=args.get("summary"),
                ),
            )
            if isinstance(result, dict) and result.get("success"):
                slot = result.get("confirmed_slot")
                connection_manager.state.appointment_booked = True
                connection_manager.state.confirmed_slot_display = (
                    (slot.get("display") if isinstance(slot, dict) else None) or result.get("message")
                )
                call_sid = getattr(connection_manager.state, "call_sid", None)
                if call_sid and result.get("confirmed_slot") is not None:
                    from services.call_records_service import sync_call_record_after_booking_action_async
                    Log.event(
                        "Syncing call record after booking",
                        {
                            "call_sid": call_sid,
                            "has_confirmed_slot": result.get("confirmed_slot") is not None,
                            "has_calendar_event_link": result.get("calendar_event_link") is not None,
                            "event_id": result.get("event_id"),
                        },
                    )
                    asyncio.create_task(
                        sync_call_record_after_booking_action_async(
                            action="booked",
                            call_sid=call_sid,
                            event_id=result.get("event_id"),
                            contact_phone=book_phone,
                            confirmed_slot=result["confirmed_slot"],
                            calendar_event_link=result.get("calendar_event_link"),
                            appointment_summary=(args.get("summary") or "").strip() or None,
                        )
                    )
                elif call_sid:
                    Log.info("Booking succeeded but no confirmed_slot to write; skipping call-record sync")
                else:
                    Log.info("Booking succeeded but no call_sid in state; skipping call-record sync")
            msg = result.get("message", "Booking completed.") if isinstance(result, dict) else "Booking completed."
            await self._send_tool_result(connection_manager, call_id, msg)
            return True

        if external_tool_registry is not None:
            registered_tool = external_tool_registry.get(str(name or ""))
            if registered_tool:
                if registered_tool.handler is None:
                    await self._send_tool_result(
                        connection_manager,
                        call_id,
                        f"Tool {name} is registered but has no handler configured.",
                    )
                    return True
                try:
                    output = await registered_tool.handler(args, connection_manager)
                except Exception as e:
                    Log.error(f"External tool {name} failed: {e}")
                    output = f"Tool {name} failed."
                await self._send_tool_result(connection_manager, call_id, output)
                return True

        return False

    async def finalize_wait_for_user(self, connection_manager, audio_service) -> None:
        """
        Cancel assistant audio already sent for a wait_for_user turn (e.g. preamble filler).
        Keeps suppression active until response.done clears it.
        """
        stream_sid = getattr(connection_manager.state, "stream_sid", None)
        if not audio_service.should_handle_interruption():
            return
        elapsed_time = audio_service.calculate_interruption_timing()
        current_item_id = audio_service.get_current_item_id()
        if elapsed_time is None or not current_item_id:
            return
        await self.handle_interruption(connection_manager, elapsed_time, current_item_id)
        if stream_sid:
            clear_message = audio_service.create_clear_message(stream_sid)
            await connection_manager.send_to_twilio(clear_message)
        audio_service.reset_interruption_state()

    async def _send_goodbye_response(self, connection_manager, text: str) -> None:
        """Send a final assistant response (audio) with the provided text before hangup.
        Uses response.create with inline instructions so the model speaks immediately without tool calls.
        """
        try:
            # Note: Recent Realtime API versions expect instructions at the top level
            # of the response.create event. Modalities are already defined in the
            # session (output_modalities=["audio"]). Sending a nested
            # response.modalities triggers an 'unknown_parameter' error.
            await connection_manager.send_to_openai({
                "type": "response.create",
                "response": {
                    "instructions": text
                }
            })
        except Exception as e:
            # If we fail to queue a goodbye, fall back to immediate hangup on next finalize
            Log.error(f"Failed to queue goodbye response: {e}")
            self._pending_goodbye = True
            self._goodbye_audio_heard = False

    def should_finalize_on_event(self, event: Dict[str, Any]) -> bool:
        """Return True if we should finalize hangup after the goodbye audio has completed.
        We only finalize on response.done (full farewell message), not on response.output_audio.done,
        so the entire farewell is generated and streamed before we start the grace sleep and hang up
        (avoids stutter/cut-off from disconnecting mid-stream).
        """
        if not (self._pending_goodbye and self._goodbye_audio_heard):
            return False
        etype = event.get('type')
        # Finalize only when the full response is done (entire farewell), not on first audio segment done
        if etype == 'response.done':
            if not self._goodbye_item_id:
                # Fallback: if we can't match IDs, but the response contains an assistant message with audio, allow finalize
                resp = event.get('response') or {}
                for item in (resp.get('output') or []):
                    if isinstance(item, dict) and item.get('type') == 'message' and item.get('role') == 'assistant':
                        for c in (item.get('content') or []):
                            if isinstance(c, dict) and c.get('type') == 'output_audio':
                                return True
                return False
            # If we do have a tracked item id, try to match it to the output item id
            resp = event.get('response') or {}
            for item in (resp.get('output') or []):
                if isinstance(item, dict) and item.get('id') == self._goodbye_item_id:
                    return True
        return False

    async def finalize_goodbye(self, connection_manager) -> None:
        """After goodbye audio is finished, wait for playback then close and optionally complete the call via REST."""
        self._pending_goodbye = False
        self._goodbye_audio_heard = False
        self._goodbye_item_id = None
        self._cancel_goodbye_watchdog()
        # Mark Twilio as closed immediately so late OpenAI events never send (avoids "WebSocket is not connected" after REST hangup).
        connection_manager.mark_twilio_closed()
        # Grace period so the caller hears the full farewell before we hang up
        grace = getattr(Config, 'END_CALL_GRACE_SECONDS', 6)
        try:
            Log.info(f"Grace sleep before hangup: {grace}s")
            await asyncio.sleep(grace)
        except Exception:
            pass
        if Config.has_twilio_credentials():
            try:
                from twilio.rest import Client
                client = Client(Config.TWILIO_ACCOUNT_SID, Config.TWILIO_AUTH_TOKEN)
                call_sid = getattr(connection_manager.state, 'call_sid', None)
                if call_sid:
                    Log.event("Completing call via Twilio REST", {"callSid": call_sid})
                    client.calls(call_sid).update(status='completed')
            except Exception as e:
                Log.error(f"Optional Twilio REST hangup failed: {e}")
        # Always attempt to close the Twilio WS as a fallback; this ends the stream
        try:
            await connection_manager.close_twilio_connection(reason="assistant completed")
        except Exception:
            pass

    def is_goodbye_pending(self) -> bool:
        """Return True if a farewell has been queued and we await its completion."""
        return self._pending_goodbye

    def mark_goodbye_audio_heard(self, item_id: Optional[str]) -> None:
        """Mark that we've begun receiving audio for the goodbye message and capture its item_id."""
        if self._pending_goodbye:
            self._goodbye_audio_heard = True
            if item_id and not self._goodbye_item_id:
                self._goodbye_item_id = item_id
            # Once audio is heard, watchdog is no longer needed
            self._cancel_goodbye_watchdog()

    def _start_goodbye_watchdog(self, connection_manager) -> None:
        """Start a watchdog that finalizes the call if no goodbye audio starts in time."""
        self._cancel_goodbye_watchdog()
        try:
            timeout = getattr(Config, 'END_CALL_WATCHDOG_SECONDS', 4)

            async def _watch():
                try:
                    await asyncio.sleep(timeout)
                    if self._pending_goodbye and not self._goodbye_audio_heard:
                        Log.info("Goodbye audio not detected in time; finalizing call")
                        await self.finalize_goodbye(connection_manager)
                except Exception:
                    pass

            self._goodbye_watchdog = asyncio.create_task(_watch())
        except Exception:
            self._goodbye_watchdog = None

    def _cancel_goodbye_watchdog(self) -> None:
        if self._goodbye_watchdog and not self._goodbye_watchdog.done():
            self._goodbye_watchdog.cancel()
        self._goodbye_watchdog = None
    
    def extract_audio_response_data(self, event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Extract relevant data from OpenAI audio response.
        
        Args:
            event: OpenAI event data
            
        Returns:
            Dictionary with audio delta and item ID, or None
        """
        if not self.event_handler.is_audio_delta_event(event):
            return None
            
        return {
            'delta': self.event_handler.extract_audio_delta(event),
            'item_id': self.event_handler.extract_item_id(event)
        }
    
    def is_speech_started(self, event: Dict[str, Any]) -> bool:
        """
        Check if event indicates user speech started (interruption).
        
        Args:
            event: OpenAI event data
            
        Returns:
            True if speech started
        """
        return self.event_handler.is_speech_started_event(event)
    
    async def handle_interruption(
        self,
        connection_manager,
        audio_end_ms: int,
        last_assistant_item: str
    ) -> None:
        """
        Handle conversation interruption by truncating the current response.
        Uses the provided audio_end_ms (capped to actual sent audio from AudioService)
        so truncation is consistent with the audio we've passed to Twilio.
        
        Args:
            connection_manager: WebSocket connection manager
            audio_end_ms: Truncation point in ms (must be <= actual assistant audio sent)
            last_assistant_item: ID of the item to truncate
        """
        if Config.SHOW_TIMING_MATH:
            print(f"Truncating item with ID: {last_assistant_item}, audio_end_ms: {audio_end_ms}")
        
        truncate_event = self.conversation_manager.create_truncate_event(
            last_assistant_item, audio_end_ms
        )
        await connection_manager.send_to_openai(truncate_event)
    
    def should_process_interruption(
        self,
        last_assistant_item: Optional[str],
        mark_queue: list,
        response_start_timestamp: Optional[int]
    ) -> bool:
        """
        Determine if an interruption should be processed.
        
        Args:
            last_assistant_item: ID of the last assistant response
            mark_queue: Queue of pending marks
            response_start_timestamp: When the current response started
            
        Returns:
            True if interruption should be handled
        """
        return self.conversation_manager.should_handle_interruption(
            last_assistant_item, mark_queue, response_start_timestamp
        )
