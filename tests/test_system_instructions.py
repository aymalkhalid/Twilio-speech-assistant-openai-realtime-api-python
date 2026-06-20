"""Tests for the clean starter prompt layer."""
from pathlib import Path

import pytest

from config import Config, rebuild_system_message
from services.openai_service import OpenAISessionManager, OpenAIService
from system_instructions import (
    DEFAULT_SYSTEM_INSTRUCTIONS_PATH,
    REQUIRED_PROMPT_PLACEHOLDERS,
    get_greeting_instruction,
    load_system_instructions,
    render_system_instructions,
)

_PROMPT_KWARGS = {
    "company_name": "Example Co",
    "agent_name": "Alex",
    "language_instruction": "# Language\nUse English.",
    "accent_instruction": "# Accent\nUse clear phone speech.",
    "reasoning_effort_instruction": "## Reasoning effort\nSession API reasoning effort is `low`.",
    "tools_availability_instruction": "## Tool Availability\nAvailable in this session:\n- `wait_for_user`",
    "call_record_instruction": "Use save_call_record for follow-up.",
    "booking_instruction": "Booking tools are disabled.",
    "transfer_instruction": "Transfer is disabled.",
    "instructions_path": "prompts/main_system_instructions.md",
}


def test_main_system_instructions_file_is_single_source_of_truth():
    assert DEFAULT_SYSTEM_INSTRUCTIONS_PATH.is_file()
    template = load_system_instructions()
    for placeholder in REQUIRED_PROMPT_PLACEHOLDERS:
        assert placeholder in template, f"missing placeholder {placeholder}"


def test_load_system_instructions_raises_when_file_missing():
    with pytest.raises(FileNotFoundError, match="System instructions file not found"):
        load_system_instructions("prompts/does-not-exist.md")


def test_prompt_file_renders_generic_voice_agent():
    prompt = render_system_instructions(**_PROMPT_KWARGS)
    lower = prompt.lower()
    assert "generic business voice agent" in lower
    assert "save_call_record" in prompt
    assert "wait_for_user" in prompt
    assert "unclear audio" in lower or "unclear" in lower
    assert "verbosity" in lower
    assert "get_availability" in prompt
    assert "plumbing" not in lower
    assert "industry" not in lower


def test_prompt_includes_openai_aligned_preamble_guidance():
    prompt = render_system_instructions(**_PROMPT_KWARGS)
    lower = prompt.lower()
    assert "when to use a preamble" in lower
    assert "when not to use a preamble" in lower
    assert "request_human_handoff" in prompt
    assert "let me think" in lower


def test_prompt_includes_reasoning_and_verbosity_guidance():
    prompt = render_system_instructions(**_PROMPT_KWARGS)
    lower = prompt.lower()
    assert "respond quickly and do not reason" in lower
    assert "do not perform extended reasoning when the caller's audio is unclear" in lower
    assert "session api reasoning effort is `low`" in lower
    assert "product or option comparisons" in lower
    assert "example comparison style" in lower


def test_prompt_includes_instruction_precision_guidance():
    prompt = render_system_instructions(**_PROMPT_KWARGS)
    lower = prompt.lower()
    assert "instruction precision" in lower
    assert "avoid broad scope" in lower
    assert "confirmation code" in lower
    assert "always ask for confirmation before doing anything" in lower


def test_prompt_includes_tool_behavior_and_failure_recovery():
    prompt = render_system_instructions(**_PROMPT_KWARGS)
    lower = prompt.lower()
    assert "tool availability" in lower
    assert "tool-call eagerness" in lower
    assert "tool failures" in lower
    assert "do not repeatedly call the same tool with the same arguments after failure" in lower
    assert "entity collection order" in lower
    assert "spelled-out characters" in lower
    assert "spoken number handling" in lower
    assert "email confirmation" in lower
    assert "entity collection workflow" in lower
    assert "never call tools with guessed, partial, ambiguous, or unconfirmed exact values" in lower
    assert "exact iso slot values" in lower


def test_build_tools_availability_instruction_lists_core_tools(monkeypatch):
    from config import _build_tools_availability_instruction

    monkeypatch.setattr("services.call_records_service.has_call_record_backend_configured", lambda: True)
    monkeypatch.setattr("services.google_calendar_booking_service.is_booking_enabled", lambda: False)
    monkeypatch.setattr(Config, "HUMAN_TRANSFER_ENABLED", False)
    monkeypatch.setattr(Config, "HUMAN_TRANSFER_URL", "")
    text = _build_tools_availability_instruction()
    assert "`wait_for_user`" in text
    assert "`save_call_record`" in text
    assert "`get_availability`" not in text


def test_tool_validation_failure_returns_structured_json():
    service = OpenAIService()
    state = type("State", (), {"caller_phone_number": "+15551234567"})()
    manager = type("Manager", (), {"state": state})()
    ok, _, error = service._normalize_and_validate_tool_args(
        "book_appointment",
        {"slot_start_iso": "not-iso", "contact_name": "Avery", "contact_phone": "+15551234567"},
        manager,
    )
    assert ok is False
    assert "slot_start_iso" in (error or "")


def test_format_tool_failure_output_includes_next_step():
    payload = OpenAIService._format_tool_failure_output("No match found.", next_step="Confirm the phone number.")
    assert '"success": false' in payload.lower()
    assert "Confirm the phone number." in payload


def test_build_language_instruction_default_only_english_policy():
    from config import _build_language_instruction

    text = _build_language_instruction("English", "default_only")
    assert "English is the default response language." in text
    assert "Do not infer language from accent alone." in text
    assert "isolated foreign words" in text
    assert "support is limited to English" in text


def test_build_language_instruction_multilingual_policy():
    from config import _build_language_instruction

    text = _build_language_instruction("English", "explicit_or_substantive")
    assert "Default to English unless the caller clearly uses another language." in text
    assert "substantive utterance" in text
    assert "Would you like me to continue in English or another language?" in text
    assert "Do not switch languages based on:" in text


def test_build_accent_instruction_keeps_language_separate():
    from config import _build_accent_instruction

    text = _build_accent_instruction("English", "neutral American", "light")
    assert "Speak English with a light neutral American accent." in text
    assert "Keep the accent stable from the first word to the last." in text
    assert "natural vowel shaping" in text
    assert "Do not change response language based on the caller's accent." in text


def test_rebuild_system_message_pins_english_by_default(monkeypatch):
    monkeypatch.setattr(Config, "ASSISTANT_LANGUAGE", "English")
    monkeypatch.setattr(Config, "LANGUAGE_SWITCH_POLICY", "default_only")
    monkeypatch.setattr(Config, "ASSISTANT_ACCENT", "neutral American")
    monkeypatch.setattr(Config, "ASSISTANT_ACCENT_STRENGTH", "light")
    rebuild_system_message()
    assert "English is the default response language." in Config.SYSTEM_MESSAGE
    assert "Speak English with a light neutral American accent." in Config.SYSTEM_MESSAGE
    assert "mirror the user" in Config.SYSTEM_MESSAGE.lower()


def test_build_reasoning_effort_instruction_for_gpt_realtime_2():
    from config import _build_reasoning_effort_instruction

    text = _build_reasoning_effort_instruction("gpt-realtime-2", "medium")
    assert "Session API reasoning effort is `medium`" in text
    assert "multi-step rescheduling" in text


def test_build_reasoning_effort_instruction_omitted_for_older_model():
    from config import _build_reasoning_effort_instruction

    assert _build_reasoning_effort_instruction("gpt-realtime-1.5", "low") == ""


def test_rebuild_system_message_includes_reasoning_effort_for_realtime_2(monkeypatch):
    monkeypatch.setattr(Config, "OPENAI_REALTIME_MODEL", "gpt-realtime-2")
    monkeypatch.setattr(Config, "REALTIME_REASONING_EFFORT", "low")
    rebuild_system_message()
    assert "Session API reasoning effort is `low`" in Config.SYSTEM_MESSAGE
    assert "respond quickly and do not reason" in Config.SYSTEM_MESSAGE.lower()


def test_slow_tool_descriptions_include_preamble_sample_phrases(monkeypatch):
    monkeypatch.setattr("services.openai_service.has_call_record_backend_configured", lambda: True)
    monkeypatch.setattr("services.openai_service.is_booking_enabled", lambda: True)
    monkeypatch.setattr(Config, "HUMAN_TRANSFER_ENABLED", True)
    tools = {tool["name"]: tool for tool in OpenAISessionManager._realtime_tools()}
    for name in (
        "get_availability",
        "list_my_bookings",
        "book_appointment",
        "edit_booking",
        "delete_booking",
        "save_call_record",
        "request_human_handoff",
    ):
        assert "Preamble sample phrases:" in tools[name]["description"], name


def test_rebuild_system_message_uses_main_prompt(monkeypatch):
    monkeypatch.setattr(Config, "COMPANY_NAME", "Example Co")
    monkeypatch.setattr(Config, "AGENT_NAME", "Alex")
    monkeypatch.setattr(Config, "SYSTEM_INSTRUCTIONS_PATH", "prompts/main_system_instructions.md")
    rebuild_system_message()
    assert "Example Co" in Config.SYSTEM_MESSAGE
    assert "Alex" in Config.SYSTEM_MESSAGE
    assert "save_call_record" in Config.SYSTEM_MESSAGE


def test_greeting_uses_company_and_agent(monkeypatch):
    monkeypatch.setenv("AGENT_NAME", "Sam")
    assert get_greeting_instruction("Example Co") == "You've reached Example Co. I'm Sam. How can I help?"


def test_session_tools_expose_save_call_record_when_backend_configured(monkeypatch):
    monkeypatch.setattr("services.openai_service.has_call_record_backend_configured", lambda: True)
    monkeypatch.setattr("services.openai_service.is_booking_enabled", lambda: False)
    tools = OpenAISessionManager._realtime_tools()
    names = {tool["name"] for tool in tools}
    assert "save_call_record" in names
    assert "wait_for_user" in names
    assert "submit_lead" not in names
    assert "end_call" in names


def test_submit_lead_legacy_alias_still_validates():
    service = OpenAIService()
    state = type("State", (), {"caller_phone_number": "+15551234567"})()
    manager = type("Manager", (), {"state": state})()
    args = {
        "contact_name": "Avery",
        "contact_phone": "caller's number",
        "issue_summary": "General question",
        "priority": "normal",
        "call_summary": "Caller asked a general question and wants follow-up.",
    }
    ok, normalized, error = service._normalize_and_validate_tool_args("submit_lead", args, manager)
    assert ok is True
    assert error is None
    assert normalized["contact_phone"] == "+15551234567"


def test_repo_has_no_yaml_files():
    root = Path(__file__).resolve().parents[1]
    yaml_files = list(root.rglob("*.yaml")) + list(root.rglob("*.yml"))
    assert yaml_files == []
