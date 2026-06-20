import os
from pathlib import Path
from typing import List
from dotenv import load_dotenv

# Load .env from the directory containing this file (project root), so config works regardless of cwd
_env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=_env_path)
# OpenAI Realtime System Instructions Structure
# Role & Objective        — who you are and what “success” means
# Personality & Tone      — the voice and style to maintain
# Language                — response language and switching rules
# Accent                  — spoken accent, separate from language
# Context                 — retrieved context, relevant info
# Reference Pronunciations — phonetic guides for tricky words
# Tools                   — names, usage rules, and preambles
# Instructions / Rules    — do’s, don’ts, and approach
# Conversation Flow       — states, goals, and transitions
# Safety & Escalation     — fallback and handoff logic
# https://platform.openai.com/docs/guides/realtime-models-prompting

# VOICE: alloy, ash, ballad, coral, echo, sage, shimmer, verse, marin, cedar
# For best quality / most human-like, OpenAI recommends: marin or cedar

DEFAULT_REALTIME_VOICE = "cedar"
SUPPORTED_REALTIME_VOICES = {
    "alloy",
    "ash",
    "ballad",
    "coral",
    "echo",
    "sage",
    "shimmer",
    "verse",
    "marin",
    "cedar",
}


def _normalize_realtime_voice(raw: str | None) -> str:
    """Return a supported OpenAI Realtime voice id."""
    voice = (raw or DEFAULT_REALTIME_VOICE).strip().lower()
    return voice if voice in SUPPORTED_REALTIME_VOICES else DEFAULT_REALTIME_VOICE


def _sanitize_prompt_control(raw: str | None, default: str, max_length: int = 64) -> str:
    """Normalize short prompt-control settings before inserting into instructions."""
    value = " ".join(str(raw or "").split())
    allowed = []
    for char in value:
        if char.isalnum() or char in " -_'/":
            allowed.append(char)
    cleaned = " ".join("".join(allowed).split()).strip(" -_'/")
    return (cleaned[:max_length].strip() or default)


def _normalize_language_switch_policy(raw: str | None) -> str:
    """Return the supported language-switch policy used by prompt builders."""
    policy = (raw or "explicit_or_substantive").strip().lower()
    if policy in {"default_only", "configured_only", "pinned", "only", "english_only"}:
        return "default_only"
    if policy in {"explicit", "explicit_only", "explicit_or_substantive", "multilingual"}:
        return "explicit_or_substantive"
    return "explicit_or_substantive"


def _normalize_accent_strength(raw: str | None) -> str:
    """Return a conservative accent-strength value for voice prompting."""
    strength = (raw or "light").strip().lower()
    if strength in {"none", "light", "moderate"}:
        return strength
    return "light"


def _build_language_instruction(language: str | None, switch_policy: str | None) -> str:
    """Build the Realtime language policy section.

    Language and accent are controlled separately so caller accent does not trigger
    accidental language switching.
    """
    language_name = _sanitize_prompt_control(language, "English", 48)
    policy = _normalize_language_switch_policy(switch_policy)
    if policy == "default_only":
        return (
            "# Language\n"
            f"{language_name} is the default response language.\n"
            "- Do not infer language from accent alone.\n"
            "- Ignore short filler sounds, backchannels, and isolated foreign words for language detection.\n"
            "- Do not switch languages based on accent, pronunciation, filler words, backchannels, names, addresses, or isolated foreign words.\n"
            f"- Keep preambles, spoken bridges, tool-related messages, and final answers in {language_name}.\n"
            f"- If the caller speaks another language, politely explain that support is limited to {language_name}.\n"
            "- If language confidence is low, ask a short clarification instead of guessing.\n"
            "- Accent adaptation must not change the response language.\n\n"
        )
    return (
        "# Language\n"
        f"Default to {language_name} unless the caller clearly uses another language.\n"
        "- Do not infer language from accent alone.\n"
        "- Ignore short filler sounds, backchannels, and isolated foreign words for language detection.\n"
        "Switch languages only when:\n"
        "- the caller explicitly asks to use another language;\n"
        "- the caller provides a substantive utterance in another language. A substantive utterance means a complete request, question, or correction in another language, not just a greeting, name, address, filler word, or borrowed phrase.\n"
        "Do not switch languages based on:\n"
        "- accent;\n"
        "- pronunciation;\n"
        "- filler words;\n"
        "- short backchannels;\n"
        "- names;\n"
        "- addresses;\n"
        "- isolated foreign words.\n"
        f"- If uncertain, ask: \"Would you like me to continue in {language_name} or another language?\"\n"
        "- Keep preambles, spoken bridges, tool-related messages, and final answers in the same language.\n"
        "- Accent adaptation must not change the response language.\n\n"
    )


def _build_accent_instruction(
    language: str | None,
    accent: str | None,
    accent_strength: str | None,
) -> str:
    """Build the Realtime accent policy section."""
    language_name = _sanitize_prompt_control(language, "English", 48)
    accent_name = _sanitize_prompt_control(accent, "neutral American", 64)
    strength = _normalize_accent_strength(accent_strength)
    if strength == "none":
        return (
            "# Accent\n"
            f"Speak clear, phone-friendly {language_name} without mimicking the caller's accent.\n"
            "- Keep pronunciation stable from the first word to the last.\n"
            "- Use a moderate pace, clear consonants, natural stress, and phone-friendly prosody.\n"
            "- Keep speech easy to understand over phone audio.\n"
            "- Do not rush.\n"
            "- Do not exaggerate delivery or imitate the caller.\n"
            "- Do not change response language based on the caller's accent.\n\n"
        )
    return (
        "# Accent\n"
        f"Speak {language_name} with a {strength} {accent_name} accent.\n"
        "- Keep the accent stable from the first word to the last.\n"
        f"- Use natural vowel shaping for a {accent_name} accent, but keep speech easy to understand.\n"
        "- Use a moderate pace, clear consonants, natural stress, and phone-friendly prosody.\n"
        "- Do not exaggerate the accent or imitate the caller.\n"
        "- Do not change response language based on the caller's accent.\n\n"
    )



def _build_call_record_instruction() -> str:
    return (
        "Use save_call_record when the conversation needs a record for follow-up, reporting, CRM/webhook delivery, or human context. "
        "Summarize what will be saved, confirm contact details when collected verbally (phone digit by digit, email character by character when needed), "
        "and include reason_for_call or issue_summary, priority, and a concise call_summary. "
        "Say a short preamble in the same turn before calling when saving may take noticeable time. "
        "Do not say the record was saved until the tool result succeeds."
    )


def _build_booking_instruction() -> str:
    if not getattr(Config, "BOOKING_ENABLED", False):
        return "Booking tools are disabled unless BOOKING_ENABLED=true and Google Calendar is configured."
    return (
        "Booking: When scheduling intent is clear, use get_availability with a short preamble, let the caller choose a slot, "
        "restate the exact day/date/time, get a clear confirmation, then call book_appointment with a short preamble. "
        "Collect and confirm exact values one at a time: callback phone digit by digit when spoken, email character by character when needed, and the chosen slot before any write. "
        "For existing bookings, confirm the callback number if needed, use list_my_bookings with a short preamble, clarify the exact booking, "
        "then confirm before edit_booking or delete_booking. "
        "If a booking tool fails, read back the phone or slot used, offer one retry, then save_call_record or handoff if it keeps failing. "
        "After a successful booking, edit, or cancellation, call save_call_record if the outcome should be tracked for follow-up."
    )


def _build_transfer_instruction() -> str:
    if not Config.is_human_transfer_enabled():
        return "Live transfer is disabled. If the caller asks for a human, save a call record for follow-up."
    return (
        "If the caller asks for a person and live transfer is available, collect their name and a brief reason, "
        "summarize the handoff, say a short preamble, then use request_human_handoff in the same turn after confirmation. "
        "If transfer fails, explain briefly, offer one retry, then use save_call_record for callback if needed."
    )


def _build_reasoning_effort_instruction(
    model: str | None,
    effort: str | None,
) -> str:
    """Inject Realtime 2 reasoning-effort guidance tied to REALTIME_REASONING_EFFORT."""
    model_name = (model or "gpt-realtime-2").strip()
    level = (effort or "low").strip().lower()
    if level not in {"minimal", "low", "medium", "high", "xhigh"}:
        level = "low"
    if model_name != "gpt-realtime-2":
        return ""
    return (
        "## Reasoning effort\n"
        f"Session API reasoning effort is `{level}` (REALTIME_REASONING_EFFORT). "
        "Use the lowest effective reasoning for each turn. Tune up only when task complexity, latency tolerance, or failure cost require it.\n"
        "- minimal: simple confirmations and lightweight checks.\n"
        "- low: customer support, intake, booking lookup, simple policy answers.\n"
        "- medium: multi-step rescheduling, ambiguous booking matches, complex routing.\n"
        "- high: high-precision writes, escalation decisions, tasks with constraints.\n"
        "- xhigh: complex planning, critical triage, high-stakes tool orchestration.\n"
    )


def _build_tools_availability_instruction() -> str:
    """List tools actually registered for this deployment (mirrors openai_service._realtime_tools)."""
    from services.call_records_service import has_call_record_backend_configured

    try:
        from services.google_calendar_booking_service import is_booking_enabled as booking_tools_enabled
    except Exception:
        def booking_tools_enabled() -> bool:
            return bool(getattr(Config, "BOOKING_ENABLED", False))

    names = ["wait_for_user", "end_call"]
    if has_call_record_backend_configured():
        names.append("save_call_record")
    if booking_tools_enabled():
        names.extend(
            [
                "get_availability",
                "book_appointment",
                "list_my_bookings",
                "edit_booking",
                "delete_booking",
            ]
        )
    if Config.is_human_transfer_enabled():
        names.append("request_human_handoff")
    tool_list = "\n".join(f"- `{name}`" for name in names)
    return (
        "## Tool Availability\n"
        "Use only the tools explicitly provided in the current tool list. Do not invent, assume, simulate, or rename tools.\n"
        f"Available in this session:\n{tool_list}\n"
        "If the caller requests an action that requires an unavailable tool, do not pretend to complete it. "
        "Briefly explain the limitation and offer the closest supported next step, such as `save_call_record` for follow-up."
    )


def _build_tools_text() -> str:
    names = ["end_call", "save_call_record"]
    if Config.is_human_transfer_enabled():
        names.append("request_human_handoff")
    if getattr(Config, "BOOKING_ENABLED", False):
        names.extend(["get_availability", "book_appointment", "list_my_bookings", "edit_booking", "delete_booking"])
    return "\n".join(f"- {name}" for name in names)


def build_system_message() -> str:
    from system_instructions import render_system_instructions

    return render_system_instructions(
        company_name=Config.COMPANY_NAME,
        agent_name=Config.AGENT_NAME,
        language_instruction=_build_language_instruction(
            Config.ASSISTANT_LANGUAGE,
            Config.LANGUAGE_SWITCH_POLICY,
        ),
        accent_instruction=_build_accent_instruction(
            Config.ASSISTANT_LANGUAGE,
            Config.ASSISTANT_ACCENT,
            Config.ASSISTANT_ACCENT_STRENGTH,
        ),
        call_record_instruction=_build_call_record_instruction(),
        booking_instruction=_build_booking_instruction(),
        transfer_instruction=_build_transfer_instruction(),
        reasoning_effort_instruction=_build_reasoning_effort_instruction(
            Config.OPENAI_REALTIME_MODEL,
            Config.REALTIME_REASONING_EFFORT,
        ),
        tools_availability_instruction=_build_tools_availability_instruction(),
        tools_text=_build_tools_text(),
        instructions_path=Config.SYSTEM_INSTRUCTIONS_PATH,
    )


class Config:
    """
    Configuration class that handles all application settings.
    Follows SRP by being responsible only for configuration management.
    """
    
    # OpenAI Configuration
    OPENAI_API_KEY: str = os.getenv('OPENAI_API_KEY')
    # Realtime API model: gpt-realtime-2 | gpt-realtime | gpt-realtime-1.5 | gpt-4o-mini-realtime-preview (see platform.openai.com rate limits)
    OPENAI_REALTIME_MODEL: str = (os.getenv('OPENAI_REALTIME_MODEL') or 'gpt-realtime-2').strip()
    _REALTIME_REASONING_EFFORT_RAW: str = (os.getenv('REALTIME_REASONING_EFFORT') or 'low').strip().lower()
    REALTIME_REASONING_EFFORT: str = (
        _REALTIME_REASONING_EFFORT_RAW
        if _REALTIME_REASONING_EFFORT_RAW in {'minimal', 'low', 'medium', 'high', 'xhigh'}
        else 'low'
    )
    TEMPERATURE: float = float(os.getenv('TEMPERATURE', 0.8))
    VOICE: str = _normalize_realtime_voice(os.getenv('VOICE', DEFAULT_REALTIME_VOICE))
    ASSISTANT_LANGUAGE: str = _sanitize_prompt_control(os.getenv('ASSISTANT_LANGUAGE'), 'English', 48)
    ASSISTANT_ACCENT: str = _sanitize_prompt_control(os.getenv('ASSISTANT_ACCENT'), 'neutral American', 64)
    ASSISTANT_ACCENT_STRENGTH: str = _normalize_accent_strength(os.getenv('ASSISTANT_ACCENT_STRENGTH') or 'light')
    LANGUAGE_SWITCH_POLICY: str = _normalize_language_switch_policy(os.getenv('LANGUAGE_SWITCH_POLICY') or 'default_only')
    COMPANY_NAME: str = os.getenv('COMPANY_NAME', 'Acme Voice Agent Demo')
    AGENT_NAME: str = (os.getenv('AGENT_NAME') or '').strip()
    AGENT_LABEL: str = (os.getenv('AGENT_LABEL') or 'generic_voice_agent').strip()
    SYSTEM_INSTRUCTIONS_PATH: str = (os.getenv('SYSTEM_INSTRUCTIONS_PATH') or 'prompts/main_system_instructions.md').strip()

    # Server Configuration
    PORT: int = int(os.getenv('PORT', 5050))
    # Business timezone (IANA, e.g. America/Los_Angeles). Used for dashboard time display and booking.
    TIMEZONE: str = (os.getenv('TIMEZONE') or 'America/Los_Angeles').strip()
    # Booking: disabled by default; requires Google Calendar config when enabled.
    BOOKING_ENABLED: bool = (os.getenv('BOOKING_ENABLED', 'false').strip().lower() in ('1', 'true', 'yes'))
    # Booking: slot length and business hours (24h HH:MM). Used by Google Calendar booking.
    BOOKING_SLOT_DURATION_MINUTES: int = max(1, int(os.getenv('BOOKING_SLOT_DURATION_MINUTES', '60')))
    BUSINESS_APPOINTMENT_OPENING_TIME: str = (os.getenv('BUSINESS_APPOINTMENT_OPENING_TIME') or '08:00').strip()
    BUSINESS_APPOINTMENT_CLOSING_TIME: str = (os.getenv('BUSINESS_APPOINTMENT_CLOSING_TIME') or '18:00').strip()
    AVAILABILITY_MAX_SLOTS_PER_BUCKET_PER_DAY: int = int(os.getenv('AVAILABILITY_MAX_SLOTS_PER_BUCKET_PER_DAY', '4'))
    # Booking weekdays: comma-separated mon,tue,wed,thu,fri,sat,sun. Empty = all days.
    BOOKING_DAYS_ENABLED: str = (os.getenv('BOOKING_DAYS_ENABLED') or '').strip()

    # Twilio REST (optional, required for programmatic hangup)
    TWILIO_ACCOUNT_SID: str | None = os.getenv('TWILIO_ACCOUNT_SID')
    TWILIO_AUTH_TOKEN: str | None = os.getenv('TWILIO_AUTH_TOKEN')
    # Call recording (REST Recordings API when using Media Stream): feature flag and callback base URL
    CALL_RECORDING_ENABLED: bool = (os.getenv('CALL_RECORDING_ENABLED', 'false').strip().lower() in ('1', 'true', 'yes'))
    RECORDING_STATUS_CALLBACK_BASE_URL: str | None = (os.getenv('RECORDING_STATUS_CALLBACK_BASE_URL') or "").strip() or None

    # Live transfer to human agent: TwiML URL to redirect the call (e.g. queue or agent). Empty = disabled.
    HUMAN_TRANSFER_URL: str | None = (os.getenv('HUMAN_TRANSFER_URL') or "").strip() or None
    # Set to 0/false/no to disable transfer even when HUMAN_TRANSFER_URL is set
    HUMAN_TRANSFER_ENABLED: bool = (os.getenv('HUMAN_TRANSFER_ENABLED', 'true').strip().lower() not in ('0', 'false', 'no'))
    # Number to dial when using the built-in /twiml/transfer-to-agent endpoint (e.g. +15551234567)
    HUMAN_TRANSFER_DIAL_NUMBER: str = (os.getenv('HUMAN_TRANSFER_DIAL_NUMBER') or "+15551234567").strip()

    # Outbound calling: dashboard-initiated campaigns via Twilio REST
    OUTBOUND_ENABLED: bool = (os.getenv('OUTBOUND_ENABLED', 'false').strip().lower() in ('1', 'true', 'yes'))
    # From number for outbound calls (falls back to Twilio incoming number if empty)
    TWILIO_OUTBOUND_NUMBER: str = (os.getenv('TWILIO_OUTBOUND_NUMBER') or '').strip()
    # Max simultaneous outbound calls per campaign (hard cap; campaign-level concurrency is capped to this)
    OUTBOUND_MAX_CONCURRENCY: int = max(1, int(os.getenv('OUTBOUND_MAX_CONCURRENCY', '3')))
    # Public base URL for outbound TwiML and status callbacks. Required for local dev (ngrok).
    # In production this is auto-detected from the request. Example: https://abc123.ngrok.io
    OUTBOUND_BASE_URL: str = (os.getenv('OUTBOUND_BASE_URL') or os.getenv('RECORDING_STATUS_CALLBACK_BASE_URL') or '').strip().rstrip('/')

    # Call-record storage: where to send saved call records. Default = webhook.
    # CALL_RECORD_BACKEND (one of): webhook | supabase | googlesheets | email | airtable | sms | telegram | slack
    # LEAD_BACKEND is accepted as a legacy alias.
    CALL_RECORD_BACKEND: str = (
        os.getenv('CALL_RECORD_BACKEND')
        or os.getenv('LEAD_BACKEND')
        or 'webhook'
    ).strip().lower()
    LEAD_BACKEND: str = CALL_RECORD_BACKEND
    # Webhook backend (default): POST to this URL when CALL_RECORD_BACKEND=webhook
    WEBHOOK_URL: str | None = os.getenv('WEBHOOK_URL')
    WEBHOOK_SECRET: str | None = os.getenv('WEBHOOK_SECRET')
    # Supabase: SUPABASE_URL, SUPABASE_KEY, SUPABASE_CALL_RECORD_TABLE (default: call_records)
    # For inserts use the secret key (SUPABASE_KEY=sb_secret_...) so RLS does not block writes.
    # Aliases: PROJECT_URL → SUPABASE_URL; key fallback: PUBLISHABLE_API_KEY, SUPABASE_PUBLISHABLE_API_KEY.
    SUPABASE_URL: str | None = os.getenv('SUPABASE_URL') or os.getenv('PROJECT_URL')
    SUPABASE_KEY: str | None = (
        os.getenv('SUPABASE_KEY')
        or os.getenv('PUBLISHABLE_API_KEY')
        or os.getenv('SUPABASE_PUBLISHABLE_API_KEY')
    )
    SUPABASE_CALL_RECORD_TABLE: str = (
        os.getenv('SUPABASE_CALL_RECORD_TABLE')
        or os.getenv('SUPABASE_LEAD_TABLE')
        or 'call_records'
    ).strip()
    SUPABASE_LEAD_TABLE: str = SUPABASE_CALL_RECORD_TABLE
    # Supabase tables for outbound campaigns (used when OUTBOUND_ENABLED=true)
    SUPABASE_OUTBOUND_CAMPAIGNS_TABLE: str = (os.getenv('SUPABASE_OUTBOUND_CAMPAIGNS_TABLE') or 'outbound_campaigns').strip()
    SUPABASE_OUTBOUND_CONTACTS_TABLE: str = (os.getenv('SUPABASE_OUTBOUND_CONTACTS_TABLE') or 'outbound_contacts').strip()
    # Dashboard auth: superadmin-defined users only (no signup). Format: user1:pass1,user2:pass2 (avoid : and , in passwords).
    # When set, /dashboard and /dashboard API routes require login or ?key=<password> / X-Dashboard-Key. Any user's password is valid for API key auth.
    DASHBOARD_USERS: str | None = (os.getenv('DASHBOARD_USERS') or "").strip() or None

    # Whisper transcription for recordings (faster-whisper). Model: tiny|base|small|medium|large-v3|distil-large-v3; empty = disabled. Default tiny for lower RAM/CPU (Cloud Run credits).
    TRANSCRIPTION_MODEL: str = (os.getenv('TRANSCRIPTION_MODEL') or "tiny").strip().lower()
    # Optional: path to a pre-downloaded Whisper model (e.g. /app/whisper_models/tiny). When set, we load from this path instead of downloading from the hub at runtime. Use when the model is bundled in the image (e.g. Cloud Run).
    WHISPER_MODEL_PATH: str | None = (os.getenv('WHISPER_MODEL_PATH') or "").strip() or None
    # Post-process transcript with OpenAI (fix errors, format as Agent/Caller dialogue). Model e.g. gpt-4o-mini. Empty = disabled.
    TRANSCRIPT_ENHANCEMENT_MODEL: str = (os.getenv('TRANSCRIPT_ENHANCEMENT_MODEL') or "gpt-4o-mini").strip()
    TRANSCRIPT_ENHANCEMENT_ENABLED: bool = (os.getenv('TRANSCRIPT_ENHANCEMENT_ENABLED', 'true').strip().lower() in ('1', 'true', 'yes'))
    # When "manual", enhancement runs only via the dashboard "Enhance transcript" button; transcribe returns Whisper-only. When "auto", enhance on every transcribe.
    TRANSCRIPT_ENHANCEMENT_MODE: str = (os.getenv('TRANSCRIPT_ENHANCEMENT_MODE', 'manual').strip().lower() or 'manual')
    # Enhancement API: temperature (0–2, lower = more deterministic); max_tokens (response length cap). .env only.
    TRANSCRIPT_ENHANCEMENT_TEMPERATURE: float = float(os.getenv('TRANSCRIPT_ENHANCEMENT_TEMPERATURE', '0.2'))
    TRANSCRIPT_ENHANCEMENT_MAX_TOKENS: int = int(os.getenv('TRANSCRIPT_ENHANCEMENT_MAX_TOKENS', '4096'))

    # AI Assistant Configuration: system message rendered from prompts/main_system_instructions.md
    SYSTEM_MESSAGE: str = ""
    
    # Voice activity detection (VAD) — used in session.update → audio.input.turn_detection.
    # See https://developers.openai.com/api/docs/guides/realtime-vad
    # Mode: "server_vad" (silence-based) or "semantic_vad" (content-based; better for noisy environments).
    VAD_MODE: str = os.getenv('VAD_MODE', 'server_vad').strip().lower()  # server_vad | semantic_vad
    # For semantic_vad only: how eager to end turn. low=let user take their time; high=chunk sooner. auto=medium.
    VAD_EAGERNESS: str = os.getenv('VAD_EAGERNESS', 'auto').strip().lower()  # low | medium | high | auto
    # Server VAD only (ignored when VAD_MODE=semantic_vad):
    # Higher threshold = less sensitive: reduces false "interruptions" from background noise or echo.
    VAD_THRESHOLD: float = float(os.getenv('VAD_THRESHOLD', 0.6))  # 0–1
    VAD_SILENCE_DURATION_MS: int = int(os.getenv('VAD_SILENCE_DURATION_MS', 600))  # ms of silence before "speech stopped"
    VAD_PREFIX_PADDING_MS: int = int(os.getenv('VAD_PREFIX_PADDING_MS', 300))  # ms of audio before speech start (avoids clipping)
    # Ignore speech_started for this many ms after we sent assistant audio (reduces echo/own-voice triggers)
    VAD_DEBOUNCE_AFTER_OUTGOING_MS: int = int(os.getenv('VAD_DEBOUNCE_AFTER_OUTGOING_MS', 1200))
    # When > 0, wait this many ms after speech_started before truncating; if speech_stopped happens before then we skip truncation (filters brief coughs/noise). 0 = disabled.
    VAD_INTERRUPTION_CONFIRM_MS: int = int(os.getenv('VAD_INTERRUPTION_CONFIRM_MS', 0))

    # OpenAI Realtime: optional transcribe caller audio for live "Caller said" logs (off by default).
    # Set e.g. REALTIME_INPUT_TRANSCRIPTION_MODEL=whisper-1 to enable. Unset or empty = disabled.
    _ritm_env = os.getenv("REALTIME_INPUT_TRANSCRIPTION_MODEL")
    REALTIME_INPUT_TRANSCRIPTION_MODEL: str = (
        "" if _ritm_env is None else (_ritm_env or "").strip()
    )
    REALTIME_INPUT_TRANSCRIPTION_LANGUAGE: str | None = (
        (os.getenv("REALTIME_INPUT_TRANSCRIPTION_LANGUAGE") or "").strip() or None
    )

    # Truncation: safety margin (ms) below our sent-audio count so API never sees audio_end_ms > its internal length
    TRUNCATION_SAFETY_MS: int = int(os.getenv('TRUNCATION_SAFETY_MS', 150))

    # Logging and Debug Configuration — high-level only (noisy events commented out)
    # Note: 'error' includes rate_limit_exceeded (TPM = tokens per minute). See docs/errors-and-debugging/RATE_LIMIT_TPM.md
    # Uncomment 'rate_limits.updated' to log usage if debugging TPM.
    LOG_EVENT_TYPES: List[str] = [
        'error',
        'response.done',
        'session.created',
        'session.updated',
        # 'response.content.done',
        # 'rate_limits.updated',
        # 'input_audio_buffer.committed',
        # 'input_audio_buffer.speech_stopped',
        # 'input_audio_buffer.speech_started',
    ]
    SHOW_TIMING_MATH: bool = False

    # End-call farewell configuration
    # Farewell instruction template: ask the model to generate the goodbye itself
    END_CALL_FAREWELL_TEMPLATE: str = (
        "Please deliver a brief, polite goodbye to the caller on behalf of {company}. "
        "You may add that we'll follow up shortly if relevant. "
        "Keep it to one short sentence. Do not call any tools; speak the goodbye now."
    )
    # Time to wait after goodbye audio is done before hanging up (lets caller hear full farewell)
    END_CALL_GRACE_SECONDS: float = float(os.getenv('END_CALL_GRACE_SECONDS', 6))
    # Watchdog: if no goodbye audio starts within this window, finalize anyway
    END_CALL_WATCHDOG_SECONDS: float = float(os.getenv('END_CALL_WATCHDOG_SECONDS', 10))
    # Realtime session renewal (preemptive reconnect before 60-minute cap)
    REALTIME_SESSION_RENEW_SECONDS: int = int(os.getenv('REALTIME_SESSION_RENEW_SECONDS', 55 * 60))

    @staticmethod
    def build_end_call_farewell(reason: str | None = None) -> str:
        """Return an instruction prompting the model to generate the goodbye itself.
        If a reason is provided, the instruction asks to briefly acknowledge it.
        """
        company = getattr(Config, 'COMPANY_NAME', None) or 'our team'
        has_reason = isinstance(reason, str) and reason.strip()
        base = Config.END_CALL_FAREWELL_TEMPLATE.format(company=company)
        if has_reason:
            return base + " Acknowledge that the caller requested to end the call."
        return base
    
    @classmethod
    def validate_required_config(cls) -> None:
        """
        Validates that all required configuration values are present.
        Raises ValueError if any required configuration is missing.
        """
        if not cls.OPENAI_API_KEY:
            raise ValueError('Missing the OpenAI API key. Please set it in the .env file.')
    
    @classmethod
    def get_openai_websocket_url(cls) -> str:
        """
        Constructs the OpenAI WebSocket URL with configuration parameters.
        """
        return (
            f"wss://api.openai.com/v1/realtime"
            f"?model={cls.OPENAI_REALTIME_MODEL}"
            f"&temperature={cls.TEMPERATURE}"
            f"&voice={cls.VOICE}"
        )
    
    @classmethod
    def get_openai_headers(cls) -> dict:
        """
        Returns the headers needed for OpenAI API authentication.
        """
        return {
            "Authorization": f"Bearer {cls.OPENAI_API_KEY}"
        }

    @classmethod
    def has_twilio_credentials(cls) -> bool:
        """Return True if Twilio credentials are configured."""
        return bool(cls.TWILIO_ACCOUNT_SID and cls.TWILIO_AUTH_TOKEN)

    @classmethod
    def is_call_recording_enabled(cls) -> bool:
        """Return True if call recording is on and callback base URL is set (so we can start recording and receive status)."""
        return bool(cls.CALL_RECORDING_ENABLED and cls.RECORDING_STATUS_CALLBACK_BASE_URL)

    @classmethod
    def get_recording_status_callback_url(cls) -> str | None:
        """Return full RecordingStatusCallback URL, or None if recording is disabled or base URL not set."""
        if not cls.is_call_recording_enabled():
            return None
        base = (cls.RECORDING_STATUS_CALLBACK_BASE_URL or "").rstrip("/")
        return f"{base}/recording-status" if base else None

    @classmethod
    def get_transfer_url(cls) -> str | None:
        """Return the URL to redirect the call to for live human transfer, or None if not configured."""
        url = (cls.HUMAN_TRANSFER_URL or "").strip()
        if url and url.startswith('"') and url.endswith('"'):
            url = url[1:-1].strip()
        if url and url.startswith("'") and url.endswith("'"):
            url = url[1:-1].strip()
        return url or None

    @classmethod
    def is_human_transfer_enabled(cls) -> bool:
        """Return True if live transfer to a human agent is enabled (URL set and not explicitly disabled)."""
        return bool(cls.get_transfer_url() and cls.HUMAN_TRANSFER_ENABLED)

    @classmethod
    def is_outbound_enabled(cls) -> bool:
        """Return True if outbound calling is enabled and Twilio + Supabase are configured."""
        return bool(
            cls.OUTBOUND_ENABLED
            and cls.has_twilio_credentials()
            and cls.SUPABASE_URL and cls.SUPABASE_KEY
        )

    @classmethod
    def get_outbound_from_number(cls) -> str:
        """Return the From number for outbound calls (TWILIO_OUTBOUND_NUMBER, or fall back to account default)."""
        return cls.TWILIO_OUTBOUND_NUMBER or ""

    @classmethod
    def is_transcription_enabled(cls) -> bool:
        """Return True if Whisper transcription is enabled (model name set and non-empty)."""
        return bool(cls.TRANSCRIPTION_MODEL and cls.TRANSCRIPTION_MODEL.strip())

    @classmethod
    def is_transcript_enhancement_enabled(cls) -> bool:
        """Return True if transcript enhancement (OpenAI mini) is enabled and API key + model are set."""
        return (
            cls.TRANSCRIPT_ENHANCEMENT_ENABLED
            and cls.OPENAI_API_KEY
            and cls.TRANSCRIPT_ENHANCEMENT_MODEL
            and cls.TRANSCRIPT_ENHANCEMENT_MODEL.strip()
        )

    @classmethod
    def is_transcript_enhancement_auto(cls) -> bool:
        """Return True if enhancement should run automatically on every transcribe (mode=auto)."""
        return (cls.TRANSCRIPT_ENHANCEMENT_MODE or "").strip().lower() == "auto"

    @classmethod
    def get_dashboard_auth(cls) -> dict:
        """Dashboard login: superadmin-defined users only (DASHBOARD_USERS). No single-password mode.
        Returns: users (list of (username, password)), signing_key (str|None), valid_keys (set for API key check).
        """
        users_raw = (cls.DASHBOARD_USERS or "").strip()
        users: list[tuple[str, str]] = []
        if users_raw:
            for part in users_raw.split(","):
                part = part.strip()
                if ":" in part:
                    u, p = part.split(":", 1)
                    u, p = u.strip(), p
                    if u and p:
                        users.append((u, p))
        if users:
            signing_key = users[0][1]
            valid_keys = {p for _, p in users}
            return {"users": users, "signing_key": signing_key, "valid_keys": valid_keys}
        return {"users": [], "signing_key": None, "valid_keys": set()}


# Initialize and validate configuration when module is imported
Config.validate_required_config()

# Normalize VAD options (must match OpenAI API)
if Config.VAD_MODE not in ('server_vad', 'semantic_vad'):
    Config.VAD_MODE = 'server_vad'
if Config.VAD_EAGERNESS not in ('low', 'medium', 'high', 'auto'):
    Config.VAD_EAGERNESS = 'auto'

def rebuild_system_message() -> None:
    """Rebuild Config.SYSTEM_MESSAGE from the main system-instructions file."""
    Config.SYSTEM_MESSAGE = build_system_message()


# Build voice-agent system message from prompts/main_system_instructions.md.
rebuild_system_message()


# Apply dynamic settings overrides from Supabase (if any). See docs/CONFIGURATION.md.
try:
    from services.dynamic_settings import load_overrides_sync, apply_overrides_to_config
    apply_overrides_to_config(load_overrides_sync())
except Exception:
    pass
