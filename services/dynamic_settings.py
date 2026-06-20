"""
Dynamic settings: load overrides from Supabase and apply to Config so dashboard Settings
can change transcription, voice, VAD, etc. without editing .env. See docs/CONFIGURATION.md.
"""
from __future__ import annotations

import os
from typing import Any

from config import (
    Config,
    _normalize_accent_strength,
    _normalize_language_switch_policy,
    _normalize_realtime_voice,
    _sanitize_prompt_control,
)
from services.log_utils import Log

# Keys the dashboard can override. Value type: "str" | "bool" | "int" | "float"
# Only used when CALL_RECORD_BACKEND=supabase and SUPABASE_URL/SUPABASE_KEY are set.
OVERRIDABLE_KEYS: dict[str, str] = {
    "TRANSCRIPTION_MODEL": "str",
    "TRANSCRIPT_ENHANCEMENT_ENABLED": "bool",
    "CALL_RECORDING_ENABLED": "bool",
    "VOICE": "str",
    "ASSISTANT_LANGUAGE": "str",
    "ASSISTANT_ACCENT": "str",
    "ASSISTANT_ACCENT_STRENGTH": "str",
    "LANGUAGE_SWITCH_POLICY": "str",
    "TEMPERATURE": "float",
    "COMPANY_NAME": "str",
    "AGENT_NAME": "str",
    "BOOKING_ENABLED": "bool",
    "BOOKING_SLOT_DURATION_MINUTES": "int",
    "BUSINESS_APPOINTMENT_OPENING_TIME": "str",
    "BUSINESS_APPOINTMENT_CLOSING_TIME": "str",
    "AVAILABILITY_MAX_SLOTS_PER_BUCKET_PER_DAY": "int",
    "BOOKING_DAYS_ENABLED": "str",
    "VAD_THRESHOLD": "float",
    "VAD_SILENCE_DURATION_MS": "int",
    "VAD_PREFIX_PADDING_MS": "int",
    "VAD_DEBOUNCE_AFTER_OUTGOING_MS": "int",
    "VAD_INTERRUPTION_CONFIRM_MS": "int",
    "VAD_MODE": "str",
    "VAD_EAGERNESS": "str",
    "HUMAN_TRANSFER_ENABLED": "bool",
    "HUMAN_TRANSFER_DIAL_NUMBER": "str",
    # Realtime / transcript models (e.g. gpt-realtime-2, gpt-realtime-1.5, gpt-4o-mini)
    "OPENAI_REALTIME_MODEL": "str",
    "REALTIME_REASONING_EFFORT": "str",
    "TRANSCRIPT_ENHANCEMENT_MODEL": "str",
    # Google Calendar booking (e.g. primary or @group.calendar.google.com)
    "GOOGLE_CALENDAR_ID": "str",
}


def _parse_value(key: str, raw: str) -> Any:
    t = OVERRIDABLE_KEYS.get(key, "str")
    s = (raw or "").strip()
    if t == "bool":
        return s.lower() in ("1", "true", "yes")
    if t == "int":
        try:
            return int(s) if s else 0
        except ValueError:
            return 0
    if t == "float":
        try:
            return float(s) if s else 0.0
        except ValueError:
            return 0.0
    return s


def load_overrides_sync() -> dict[str, str]:
    """Load key-value overrides from Supabase app_settings table. Returns {} if not configured or on error."""
    if (Config.CALL_RECORD_BACKEND or "").strip().lower() != "supabase":
        return {}
    if not Config.SUPABASE_URL or not Config.SUPABASE_KEY:
        return {}
    try:
        from supabase import create_client
        client = create_client(Config.SUPABASE_URL.strip(), Config.SUPABASE_KEY.strip())
        # Use a small table name; typically in the same DB as call records
        r = client.table("app_settings").select("key, value").execute()
        rows = getattr(r, "data", None) or []
        return {row["key"]: row.get("value") or "" for row in rows if row.get("key") in OVERRIDABLE_KEYS}
    except Exception as e:
        Log.error(f"Dynamic settings load error: {e}")
        return {}


def save_overrides_sync(updates: dict[str, Any]) -> bool:
    """Upsert overrides into Supabase app_settings. Only keys in OVERRIDABLE_KEYS are written. Returns True on success."""
    if (Config.CALL_RECORD_BACKEND or "").strip().lower() != "supabase":
        return False
    if not Config.SUPABASE_URL or not Config.SUPABASE_KEY:
        return False
    allowed = {k: v for k, v in updates.items() if k in OVERRIDABLE_KEYS}
    if not allowed:
        return True
    try:
        from supabase import create_client
        client = create_client(Config.SUPABASE_URL.strip(), Config.SUPABASE_KEY.strip())
        for key, val in allowed.items():
            client.table("app_settings").upsert({"key": key, "value": str(val)}, on_conflict="key").execute()
        return True
    except Exception as e:
        Log.error(f"Dynamic settings save error: {e}")
        return False


def apply_overrides_to_config(overrides: dict[str, str]) -> None:
    """Apply loaded overrides to Config and rebuild system message if company/agent profile changed."""
    if not overrides:
        return
    prompt_needs_rebuild = False
    for key, raw in overrides.items():
        if key not in OVERRIDABLE_KEYS:
            continue
        try:
            val = _parse_value(key, raw)
            if key == "TRANSCRIPTION_MODEL":
                Config.TRANSCRIPTION_MODEL = (val or "tiny").strip().lower() if isinstance(val, str) else getattr(Config, "TRANSCRIPTION_MODEL", "tiny")
            elif key == "TRANSCRIPT_ENHANCEMENT_ENABLED":
                Config.TRANSCRIPT_ENHANCEMENT_ENABLED = bool(val)
            elif key == "CALL_RECORDING_ENABLED":
                Config.CALL_RECORDING_ENABLED = bool(val)
            elif key == "VOICE":
                Config.VOICE = _normalize_realtime_voice(val if isinstance(val, str) else None)
            elif key == "ASSISTANT_LANGUAGE":
                Config.ASSISTANT_LANGUAGE = _sanitize_prompt_control(val if isinstance(val, str) else None, "English", 48)
                prompt_needs_rebuild = True
            elif key == "ASSISTANT_ACCENT":
                Config.ASSISTANT_ACCENT = _sanitize_prompt_control(val if isinstance(val, str) else None, "neutral American", 64)
                prompt_needs_rebuild = True
            elif key == "ASSISTANT_ACCENT_STRENGTH":
                Config.ASSISTANT_ACCENT_STRENGTH = _normalize_accent_strength(val if isinstance(val, str) else None)
                prompt_needs_rebuild = True
            elif key == "LANGUAGE_SWITCH_POLICY":
                Config.LANGUAGE_SWITCH_POLICY = _normalize_language_switch_policy(val if isinstance(val, str) else None)
                prompt_needs_rebuild = True
            elif key == "TEMPERATURE":
                Config.TEMPERATURE = float(val) if isinstance(val, (int, float)) else Config.TEMPERATURE
            elif key == "COMPANY_NAME":
                Config.COMPANY_NAME = (val or "").strip() or Config.COMPANY_NAME
                prompt_needs_rebuild = True
            elif key == "AGENT_NAME":
                Config.AGENT_NAME = (val or "").strip()
                prompt_needs_rebuild = True
            elif key == "BOOKING_ENABLED":
                Config.BOOKING_ENABLED = bool(val)
                prompt_needs_rebuild = True
            elif key == "BOOKING_SLOT_DURATION_MINUTES":
                setattr(Config, "BOOKING_SLOT_DURATION_MINUTES", max(1, int(val)) if isinstance(val, (int, float)) else 60)
            elif key == "BUSINESS_APPOINTMENT_OPENING_TIME":
                setattr(Config, "BUSINESS_APPOINTMENT_OPENING_TIME", (val or "08:00").strip() if isinstance(val, str) else "08:00")
            elif key == "BUSINESS_APPOINTMENT_CLOSING_TIME":
                setattr(Config, "BUSINESS_APPOINTMENT_CLOSING_TIME", (val or "18:00").strip() if isinstance(val, str) else "18:00")
            elif key == "AVAILABILITY_MAX_SLOTS_PER_BUCKET_PER_DAY":
                setattr(Config, "AVAILABILITY_MAX_SLOTS_PER_BUCKET_PER_DAY", int(val) if isinstance(val, (int, float)) else 4)
            elif key == "BOOKING_DAYS_ENABLED":
                setattr(Config, "BOOKING_DAYS_ENABLED", (val or "").strip() if isinstance(val, str) else "")
            elif key == "VAD_THRESHOLD":
                Config.VAD_THRESHOLD = float(val) if isinstance(val, (int, float)) else Config.VAD_THRESHOLD
            elif key == "VAD_SILENCE_DURATION_MS":
                Config.VAD_SILENCE_DURATION_MS = int(val) if isinstance(val, (int, float)) else Config.VAD_SILENCE_DURATION_MS
            elif key == "VAD_PREFIX_PADDING_MS":
                Config.VAD_PREFIX_PADDING_MS = int(val) if isinstance(val, (int, float)) else Config.VAD_PREFIX_PADDING_MS
            elif key == "VAD_DEBOUNCE_AFTER_OUTGOING_MS":
                Config.VAD_DEBOUNCE_AFTER_OUTGOING_MS = int(val) if isinstance(val, (int, float)) else Config.VAD_DEBOUNCE_AFTER_OUTGOING_MS
            elif key == "VAD_INTERRUPTION_CONFIRM_MS":
                Config.VAD_INTERRUPTION_CONFIRM_MS = int(val) if isinstance(val, (int, float)) else Config.VAD_INTERRUPTION_CONFIRM_MS
            elif key == "VAD_MODE":
                Config.VAD_MODE = (val or "server_vad").strip().lower() if isinstance(val, str) else Config.VAD_MODE
            elif key == "VAD_EAGERNESS":
                Config.VAD_EAGERNESS = (val or "auto").strip().lower() if isinstance(val, str) else Config.VAD_EAGERNESS
            elif key == "HUMAN_TRANSFER_ENABLED":
                Config.HUMAN_TRANSFER_ENABLED = bool(val)
            elif key == "HUMAN_TRANSFER_DIAL_NUMBER":
                Config.HUMAN_TRANSFER_DIAL_NUMBER = (val or "").strip() or getattr(Config, "HUMAN_TRANSFER_DIAL_NUMBER", "+15551234567")
            elif key == "OPENAI_REALTIME_MODEL":
                Config.OPENAI_REALTIME_MODEL = (val or "gpt-realtime-2").strip() if isinstance(val, str) else Config.OPENAI_REALTIME_MODEL
            elif key == "REALTIME_REASONING_EFFORT":
                effort = (val or "low").strip().lower() if isinstance(val, str) else "low"
                Config.REALTIME_REASONING_EFFORT = effort if effort in {"minimal", "low", "medium", "high", "xhigh"} else "low"
            elif key == "TRANSCRIPT_ENHANCEMENT_MODEL":
                Config.TRANSCRIPT_ENHANCEMENT_MODEL = (val or "gpt-4o-mini").strip() if isinstance(val, str) else Config.TRANSCRIPT_ENHANCEMENT_MODEL
            elif key == "GOOGLE_CALENDAR_ID":
                os.environ["GOOGLE_CALENDAR_ID"] = (val or "").strip() if isinstance(val, str) else os.environ.get("GOOGLE_CALENDAR_ID", "")
        except Exception as e:
            Log.error(f"Dynamic settings apply {key}: {e}")
    if prompt_needs_rebuild:
        _rebuild_system_message()
    # Sync to os.environ so prompt and booking helpers see overrides
    for key in (
        "AGENT_NAME",
        "COMPANY_NAME",
        "BOOKING_ENABLED",
        "ASSISTANT_LANGUAGE",
        "ASSISTANT_ACCENT",
        "ASSISTANT_ACCENT_STRENGTH",
        "LANGUAGE_SWITCH_POLICY",
        "BOOKING_SLOT_DURATION_MINUTES",
        "BUSINESS_APPOINTMENT_OPENING_TIME",
        "BUSINESS_APPOINTMENT_CLOSING_TIME",
        "AVAILABILITY_MAX_SLOTS_PER_BUCKET_PER_DAY",
        "BOOKING_DAYS_ENABLED",
    ):
        if key in overrides:
            val = getattr(Config, key, None)
            if val is not None:
                os.environ[key] = str(val)


def _rebuild_system_message() -> None:
    """Rebuild Config.SYSTEM_MESSAGE from the main system-instructions file."""
    try:
        from config import rebuild_system_message
        rebuild_system_message()
    except Exception as e:
        Log.error(f"Rebuild system message: {e}")


def get_effective_settings() -> dict[str, Any]:
    """Return current effective value for each overridable key (from Config / os.environ). For GET /settings."""
    out: dict[str, Any] = {}
    for key in OVERRIDABLE_KEYS:
        if key == "BOOKING_SLOT_DURATION_MINUTES":
            out[key] = getattr(Config, "BOOKING_SLOT_DURATION_MINUTES", 60)
        elif key == "BUSINESS_APPOINTMENT_OPENING_TIME":
            out[key] = getattr(Config, "BUSINESS_APPOINTMENT_OPENING_TIME", "08:00")
        elif key == "BUSINESS_APPOINTMENT_CLOSING_TIME":
            out[key] = getattr(Config, "BUSINESS_APPOINTMENT_CLOSING_TIME", "18:00")
        elif key == "AVAILABILITY_MAX_SLOTS_PER_BUCKET_PER_DAY":
            out[key] = getattr(Config, "AVAILABILITY_MAX_SLOTS_PER_BUCKET_PER_DAY", 4)
        elif key == "BOOKING_DAYS_ENABLED":
            out[key] = getattr(Config, "BOOKING_DAYS_ENABLED", "") or ""
        elif key == "GOOGLE_CALENDAR_ID":
            out[key] = os.environ.get("GOOGLE_CALENDAR_ID", "") or ""
        else:
            out[key] = getattr(Config, key, None)
    return out
