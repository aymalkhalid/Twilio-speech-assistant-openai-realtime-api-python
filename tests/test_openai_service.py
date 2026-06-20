"""Focused tests for OpenAI service session payload and tool idempotency."""

import asyncio
import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

from dotenv import load_dotenv

load_dotenv()

# Ensure project root is on path.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config
from services.openai_service import OpenAISessionManager, OpenAIService
import services.openai_service as openai_service_module
import services.webhook_service as webhook_service_module
import services.call_records_service as call_records_service_module


def test_create_session_update_uses_configured_realtime_model(monkeypatch):
    """session.update model must follow Config.OPENAI_REALTIME_MODEL."""
    monkeypatch.setattr(Config, "OPENAI_REALTIME_MODEL", "gpt-realtime-1.5")
    monkeypatch.setattr(Config, "REALTIME_INPUT_TRANSCRIPTION_MODEL", "")
    payload = OpenAISessionManager.create_session_update()
    assert payload["session"]["model"] == "gpt-realtime-1.5"


def test_create_session_update_includes_reasoning_effort_for_gpt_realtime_2(monkeypatch):
    """gpt-realtime-2 sessions should carry the configured reasoning effort."""
    monkeypatch.setattr(Config, "OPENAI_REALTIME_MODEL", "gpt-realtime-2")
    monkeypatch.setattr(Config, "REALTIME_REASONING_EFFORT", "low")
    monkeypatch.setattr(Config, "REALTIME_INPUT_TRANSCRIPTION_MODEL", "")
    payload = OpenAISessionManager.create_session_update()
    assert payload["session"]["reasoning"] == {"effort": "low"}


def test_create_session_update_omits_reasoning_effort_for_older_realtime_model(monkeypatch):
    """Older realtime models should not receive gpt-realtime-2 reasoning config."""
    monkeypatch.setattr(Config, "OPENAI_REALTIME_MODEL", "gpt-realtime-1.5")
    monkeypatch.setattr(Config, "REALTIME_REASONING_EFFORT", "low")
    monkeypatch.setattr(Config, "REALTIME_INPUT_TRANSCRIPTION_MODEL", "")
    payload = OpenAISessionManager.create_session_update()
    assert "reasoning" not in payload["session"]


def test_booking_runtime_policy_hint_marks_sunday_closed(monkeypatch):
    monkeypatch.setattr(openai_service_module, "is_booking_enabled", lambda: True)
    monkeypatch.setattr(Config, "TIMEZONE", "UTC")
    monkeypatch.setattr(Config, "BOOKING_DAYS_ENABLED", "mon,tue,wed,thu,fri,sat")
    monkeypatch.setattr(Config, "BUSINESS_APPOINTMENT_OPENING_TIME", "08:00")
    monkeypatch.setattr(Config, "BUSINESS_APPOINTMENT_CLOSING_TIME", "23:00")

    hint = openai_service_module._booking_runtime_policy_hint(
        now_utc=datetime(2026, 4, 19, 12, 0, tzinfo=timezone.utc)
    )
    assert "BOOKING_POLICY_RUNTIME:" in hint
    assert "today=2026-04-19(sun)" in hint
    assert "today_bookable=false" in hint


def test_create_session_update_appends_booking_runtime_policy_hint(monkeypatch):
    monkeypatch.setattr(openai_service_module, "_booking_runtime_policy_hint", lambda now_utc=None: "BOOKING_POLICY_RUNTIME: test")
    monkeypatch.setattr(Config, "SYSTEM_MESSAGE", "BASE_MESSAGE")
    payload = OpenAISessionManager.create_session_update()
    instructions = payload["session"]["instructions"]
    assert "BASE_MESSAGE" in instructions
    assert "BOOKING_POLICY_RUNTIME: test" in instructions


def test_create_session_update_includes_input_transcription_when_model_set(monkeypatch):
    monkeypatch.setattr(Config, "REALTIME_INPUT_TRANSCRIPTION_MODEL", "whisper-1")
    monkeypatch.setattr(Config, "REALTIME_INPUT_TRANSCRIPTION_LANGUAGE", None)
    payload = OpenAISessionManager.create_session_update()
    inp = payload["session"]["audio"]["input"]
    assert inp["transcription"] == {"model": "whisper-1"}


def test_create_session_update_includes_transcription_language_when_set(monkeypatch):
    monkeypatch.setattr(Config, "REALTIME_INPUT_TRANSCRIPTION_MODEL", "whisper-1")
    monkeypatch.setattr(Config, "REALTIME_INPUT_TRANSCRIPTION_LANGUAGE", "en")
    payload = OpenAISessionManager.create_session_update()
    assert payload["session"]["audio"]["input"]["transcription"] == {"model": "whisper-1", "language": "en"}


def test_create_session_update_omits_input_transcription_when_model_empty(monkeypatch):
    monkeypatch.setattr(Config, "REALTIME_INPUT_TRANSCRIPTION_MODEL", "")
    payload = OpenAISessionManager.create_session_update()
    inp = payload["session"]["audio"]["input"]
    assert "transcription" not in inp


def test_initial_conversation_item_respects_configured_greeting_only(monkeypatch):
    """First response should not add an extra service menu beyond the configured greeting."""
    monkeypatch.setattr(Config, "COMPANY_NAME", "Example HVAC")
    monkeypatch.setattr(
        openai_service_module,
        "get_greeting_instruction",
        lambda company: f"Thanks for calling {company}. I'm Sam, the voice agent. May I get your name?",
    )
    monkeypatch.setattr(openai_service_module, "get_agent_name", lambda: "Sam")

    payload = OpenAISessionManager.create_initial_conversation_item()
    text = payload["item"]["content"][0]["text"]

    assert "May I get your name?" in text
    assert "Say only that greeting in the first response" in text
    assert "Do not add a service list or extra intake question" in text
    assert "Treat it as an open question" not in text


class _DummyConnectionManager:
    """Minimal connection manager stub for tool-call tests."""

    def __init__(self, call_sid=None):
        self.state = SimpleNamespace(
            call_sid=call_sid,
            caller_phone_number=None,
            lead_submitted=False,
            appointment_booked=False,
            confirmed_slot_display=None,
            priority=None,
            resolved_business_lead_id=None,
            call_record_saved=False,
            resolved_call_record_id=None,
        )
        self.sent_to_openai = []

    async def send_to_openai(self, message):
        self.sent_to_openai.append(message)

    def mark_twilio_closed(self):
        return None


class _ImmediateExecutorLoop:
    """Run executor work inline so unit tests don't depend on threadpool shutdown."""

    async def run_in_executor(self, executor, func):
        return func()


def test_tool_call_idempotency_dedupes_same_call_id():
    """Same call_id should run only once (completed + done duplicate path)."""
    service = OpenAIService()
    connection_manager = _DummyConnectionManager(call_sid=None)
    tool_call = {
        "name": "request_human_handoff",
        "arguments": {"reason": "caller asked for a human"},
        "call_id": "call_123",
    }

    async def _run():
        assert await service.maybe_handle_tool_call(connection_manager, tool_call) is True
        assert await service.maybe_handle_tool_call(connection_manager, tool_call) is True

    asyncio.run(_run())
    # One _send_tool_result => 2 messages (function_call_output + response.create)
    assert len(connection_manager.sent_to_openai) == 2


def test_tool_call_idempotency_ignores_unreliable_default_call_id():
    """Fallback call_id='default' should not suppress future distinct tool calls."""
    service = OpenAIService()
    connection_manager = _DummyConnectionManager(call_sid=None)
    tool_call = {
        "name": "request_human_handoff",
        "arguments": {"reason": "caller asked for a human"},
        "call_id": "default",
    }

    async def _run():
        assert await service.maybe_handle_tool_call(connection_manager, tool_call) is True
        assert await service.maybe_handle_tool_call(connection_manager, tool_call) is True

    asyncio.run(_run())
    # Both calls execute because "default" is not a reliable id => 4 messages total.
    assert len(connection_manager.sent_to_openai) == 4


def _tool_outputs(connection_manager):
    """Collect function_call_output strings sent back to OpenAI."""
    outputs = []
    for m in connection_manager.sent_to_openai:
        if (
            isinstance(m, dict)
            and m.get("type") == "conversation.item.create"
            and isinstance(m.get("item"), dict)
            and m["item"].get("type") == "function_call_output"
        ):
            outputs.append(m["item"].get("output") or "")
    return outputs


async def _drain_background_tasks():
    """Wait for fire-and-forget tasks spawned by the handler before the loop closes."""
    current = asyncio.current_task()
    pending = [task for task in asyncio.all_tasks() if task is not current and not task.done()]
    if pending:
        await asyncio.gather(*pending)


def test_submit_lead_validation_blocks_missing_required_fields():
    """Validation should reject malformed submit_lead payloads before side effects."""
    service = OpenAIService()
    connection_manager = _DummyConnectionManager(call_sid="CA_test")
    connection_manager.state.caller_phone_number = "+15551234567"
    tool_call = {
        "name": "submit_lead",
        "arguments": {
            "contact_name": "Alex",
            "contact_phone": "caller's number",
            "issue_summary": "Water leak",
            "priority": "emergency",
            # call_summary intentionally missing
        },
        "call_id": "call_submit_bad_1",
    }

    async def _run():
        assert await service.maybe_handle_tool_call(connection_manager, tool_call) is True

    asyncio.run(_run())
    outputs = _tool_outputs(connection_manager)
    assert outputs, "Expected a validation message via function_call_output"
    assert "Missing required field(s) for submit_lead: call_summary." in outputs[0]
    assert connection_manager.state.lead_submitted is False


def test_get_availability_validation_rejects_invalid_for_date():
    """for_date must be YYYY-MM-DD when provided."""
    service = OpenAIService()
    connection_manager = _DummyConnectionManager(call_sid=None)
    tool_call = {
        "name": "get_availability",
        "arguments": {"days_ahead": 7, "for_date": "03/05/2026"},
        "call_id": "call_availability_bad_date",
    }

    async def _run():
        assert await service.maybe_handle_tool_call(connection_manager, tool_call) is True

    asyncio.run(_run())
    outputs = _tool_outputs(connection_manager)
    assert outputs, "Expected a validation message via function_call_output"
    assert "Invalid for_date for get_availability; expected YYYY-MM-DD." in outputs[0]


def test_get_availability_reports_closed_weekday(monkeypatch):
    """Disabled weekdays should produce an explicit 'closed' message, not a generic no-slots response."""
    service = OpenAIService()
    connection_manager = _DummyConnectionManager(call_sid=None)

    def _fake_booking_get_availability(*, days_ahead=7, for_date=None):
        assert days_ahead == 7
        assert for_date == "2026-04-26"
        return {
            "by_day": {},
            "slots_flat": [],
            "closed_day": {
                "date": "2026-04-26",
                "weekday": "sun",
                "weekday_display": "Sunday",
                "allowed_days": ["mon", "tue", "wed", "thu", "fri", "sat"],
                "closed_days": ["sun"],
            },
        }

    monkeypatch.setattr(openai_service_module, "booking_get_availability", _fake_booking_get_availability)
    monkeypatch.setattr(openai_service_module.asyncio, "get_event_loop", lambda: _ImmediateExecutorLoop())

    tool_call = {
        "name": "get_availability",
        "arguments": {"for_date": "2026-04-26"},
        "call_id": "call_availability_closed_day",
    }

    async def _run():
        assert await service.maybe_handle_tool_call(connection_manager, tool_call) is True

    asyncio.run(_run())
    outputs = _tool_outputs(connection_manager)
    assert outputs, "Expected a closed-day guidance message via function_call_output"
    assert "Sunday" in outputs[0]
    assert "closed for booking" in outputs[0]
    assert "open on Monday" in outputs[0]


def test_submit_lead_validation_uses_caller_phone_fallback():
    """Placeholder contact_phone should normalize to caller_phone before required checks."""
    service = OpenAIService()
    connection_manager = _DummyConnectionManager(call_sid=None)
    connection_manager.state.caller_phone_number = "+15559876543"

    ok, normalized, err = service._normalize_and_validate_tool_args(
        "submit_lead",
        {
            "contact_name": "Taylor",
            "contact_phone": "caller's number",
            "issue_summary": "Need service",
            "priority": "routine",
            "call_summary": "Caller requested service and callback.",
        },
        connection_manager,
    )

    assert ok is True
    assert err is None
    assert normalized["contact_phone"] == "+15559876543"


def test_submit_lead_validation_corrects_phone_when_context_says_calling_number():
    """If the tool text says callback from the calling number, caller_phone is source of truth."""
    service = OpenAIService()
    connection_manager = _DummyConnectionManager(call_sid=None)
    connection_manager.state.caller_phone_number = "+12185953862"

    ok, normalized, err = service._normalize_and_validate_tool_args(
        "submit_lead",
        {
            "contact_name": "Nano Party",
            "contact_phone": "218-595-3062",
            "issue_summary": "Caller preferred human callback.",
            "priority": "routine",
            "call_summary": "Caller requested a callback from the number they are calling from.",
        },
        connection_manager,
    )

    assert ok is True
    assert err is None
    assert normalized["contact_phone"] == "+12185953862"


def test_submit_lead_validation_corrects_known_prompt_example_phone():
    """A copied prompt-example phone should not beat the Twilio caller_phone."""
    service = OpenAIService()
    connection_manager = _DummyConnectionManager(call_sid=None)
    connection_manager.state.caller_phone_number = "+12185953862"

    ok, normalized, err = service._normalize_and_validate_tool_args(
        "submit_lead",
        {
            "contact_name": "Nano Party",
            "contact_phone": "218-595-3061",
            "issue_summary": "Caller preferred human callback.",
            "priority": "routine",
            "call_summary": "Caller asked to speak with a team member instead of the voice agent.",
        },
        connection_manager,
    )

    assert ok is True
    assert err is None
    assert normalized["contact_phone"] == "+12185953862"


def test_submit_lead_validation_preserves_explicit_alternate_callback_number():
    """A caller-provided different callback number should not be overwritten."""
    service = OpenAIService()
    connection_manager = _DummyConnectionManager(call_sid=None)
    connection_manager.state.caller_phone_number = "+12185953862"

    ok, normalized, err = service._normalize_and_validate_tool_args(
        "submit_lead",
        {
            "contact_name": "Nano Party",
            "contact_phone": "218-595-3061",
            "issue_summary": "Caller preferred human callback.",
            "priority": "routine",
            "call_summary": "Caller provided a different number for the callback.",
        },
        connection_manager,
    )

    assert ok is True
    assert err is None
    assert normalized["contact_phone"] == "218-595-3061"


def test_list_my_bookings_includes_summary_for_disambiguation(monkeypatch):
    service = OpenAIService()
    connection_manager = _DummyConnectionManager(call_sid=None)

    def _fake_booking_list_my_bookings(**kwargs):
        return [
            {
                "event_id": "evt_office_1",
                "display": "Mon Apr 20 at 01:00 PM",
                "summary": "Office cleaning - Main Boulevard",
                "caller_name": "Amel",
                "visit_summary": "Standard residential cleaning",
                "service_type": "cleaning",
            }
        ]

    monkeypatch.setattr(openai_service_module, "booking_list_my_bookings", _fake_booking_list_my_bookings)
    monkeypatch.setattr(openai_service_module.asyncio, "get_event_loop", lambda: _ImmediateExecutorLoop())

    tool_call = {
        "name": "list_my_bookings",
        "arguments": {
            "contact_phone": "+12185953061",
            "contact_name": "Emil",
        },
        "call_id": "call_list_summary_1",
    }

    async def _run():
        assert await service.maybe_handle_tool_call(connection_manager, tool_call) is True

    asyncio.run(_run())
    outputs = _tool_outputs(connection_manager)
    assert outputs
    assert "Office cleaning - Main Boulevard" in outputs[0]
    assert "booked under Amel" in outputs[0]
    assert "details: Standard residential cleaning" in outputs[0]
    assert "evt_office_1" in outputs[0]


def test_list_my_bookings_ranks_best_candidate_from_booking_hint(monkeypatch):
    service = OpenAIService()
    connection_manager = _DummyConnectionManager(call_sid=None)

    def _fake_booking_list_my_bookings(**kwargs):
        return [
            {
                "event_id": "evt_8am",
                "display": "Mon Apr 20 at 08:00 AM",
                "summary": "Standard residential cleaning",
                "caller_name": "Amel",
                "visit_summary": "Standard residential cleaning",
                "service_type": "cleaning",
            },
            {
                "event_id": "evt_4pm",
                "display": "Mon Apr 20 at 04:00 PM",
                "summary": "Deep clean apartment",
                "caller_name": "Emil",
                "visit_summary": "Deep clean appointment for Emil's apartment",
                "service_type": "deep clean",
            },
        ]

    monkeypatch.setattr(openai_service_module, "booking_list_my_bookings", _fake_booking_list_my_bookings)
    monkeypatch.setattr(openai_service_module.asyncio, "get_event_loop", lambda: _ImmediateExecutorLoop())

    tool_call = {
        "name": "list_my_bookings",
        "arguments": {
            "contact_phone": "+12185953061",
            "contact_name": "Emil",
            "booking_hint": "8 AM standard residential cleaning",
        },
        "call_id": "call_ranked_list_1",
    }

    async def _run():
        assert await service.maybe_handle_tool_call(connection_manager, tool_call) is True

    asyncio.run(_run())
    outputs = _tool_outputs(connection_manager)
    assert outputs
    assert "Best candidate based on caller details" in outputs[0]
    assert "BEST: Mon Apr 20 at 08:00 AM" in outputs[0]
    assert "evt_8am" in outputs[0]
    assert "requested time match" in outputs[0]
    assert "Mon Apr 20 at 04:00 PM" in outputs[0]
    assert "evt_4pm" in outputs[0]


def test_list_my_bookings_requests_clarification_when_no_strong_match(monkeypatch):
    service = OpenAIService()
    connection_manager = _DummyConnectionManager(call_sid=None)

    def _fake_booking_list_my_bookings(**kwargs):
        return [
            {
                "event_id": "evt_8am",
                "display": "Mon Apr 20 at 08:00 AM",
                "summary": "Standard residential cleaning",
                "caller_name": "Amel",
            },
            {
                "event_id": "evt_4pm",
                "display": "Mon Apr 20 at 04:00 PM",
                "summary": "Deep clean apartment",
                "caller_name": "Emil",
            },
        ]

    monkeypatch.setattr(openai_service_module, "booking_list_my_bookings", _fake_booking_list_my_bookings)
    monkeypatch.setattr(openai_service_module.asyncio, "get_event_loop", lambda: _ImmediateExecutorLoop())

    tool_call = {
        "name": "list_my_bookings",
        "arguments": {
            "contact_phone": "+12185953061",
        },
        "call_id": "call_ranked_list_2",
    }

    async def _run():
        assert await service.maybe_handle_tool_call(connection_manager, tool_call) is True

    asyncio.run(_run())
    outputs = _tool_outputs(connection_manager)
    assert outputs
    assert "No strong match from caller details yet; ask a clarifying question before edit/delete." in outputs[0]
    assert "BEST:" not in outputs[0]


def test_list_my_bookings_uses_fuzzy_name_variant_as_soft_signal(monkeypatch):
    service = OpenAIService()
    connection_manager = _DummyConnectionManager(call_sid=None)

    def _fake_booking_list_my_bookings(**kwargs):
        return [
            {
                "event_id": "evt_variant",
                "display": "Mon Apr 20 at 08:00 AM",
                "summary": "Move-out cleaning",
                "caller_name": "Aimal",
            },
            {
                "event_id": "evt_other",
                "display": "Mon Apr 20 at 04:00 PM",
                "summary": "Office cleaning",
                "caller_name": "Khan",
            },
        ]

    monkeypatch.setattr(openai_service_module, "booking_list_my_bookings", _fake_booking_list_my_bookings)
    monkeypatch.setattr(openai_service_module.asyncio, "get_event_loop", lambda: _ImmediateExecutorLoop())

    tool_call = {
        "name": "list_my_bookings",
        "arguments": {
            "contact_phone": "+12185953061",
            "contact_name": "Aymal",
        },
        "call_id": "call_ranked_list_fuzzy_1",
    }

    async def _run():
        assert await service.maybe_handle_tool_call(connection_manager, tool_call) is True

    asyncio.run(_run())
    outputs = _tool_outputs(connection_manager)
    assert outputs
    assert "Best candidate based on caller details" in outputs[0]
    assert "BEST: Mon Apr 20 at 08:00 AM" in outputs[0]
    assert "booked-under name is a close variant (Aimal)" in outputs[0]
    assert "evt_variant" in outputs[0]
    assert "evt_other" in outputs[0]


def test_wait_for_user_tool_description_matches_openai_guidance():
    tools = OpenAISessionManager._realtime_tools()
    by_name = {tool["name"]: tool for tool in tools if tool.get("type") == "function"}
    wait_tool = by_name["wait_for_user"]
    assert wait_tool["parameters"] == {"type": "object", "properties": {}, "required": []}
    assert "does not need a spoken response" in wait_tool["description"]
    assert "hold music" in wait_tool["description"]


def test_wait_for_user_sends_output_without_response_create():
    """wait_for_user should complete the turn silently (no follow-up response.create)."""
    service = OpenAIService()
    connection_manager = _DummyConnectionManager(call_sid="CA123")
    tool_call = {
        "name": "wait_for_user",
        "arguments": {},
        "call_id": "call_wait_1",
    }

    async def _run():
        assert await service.maybe_handle_tool_call(connection_manager, tool_call) is True

    asyncio.run(_run())
    outputs = _tool_outputs(connection_manager)
    assert outputs == ["OK. Stay silent and keep listening. Do not produce a spoken reply."]
    response_creates = [
        m for m in connection_manager.sent_to_openai
        if isinstance(m, dict) and m.get("type") == "response.create"
    ]
    assert response_creates == []
    assert connection_manager.state.wait_for_user_count == 1


def test_wait_for_user_delta_enables_audio_suppression():
    service = OpenAIService()
    assert service.should_suppress_assistant_audio() is False
    service.accumulate_tool_call({
        "type": "response.function_call.arguments.delta",
        "name": "wait_for_user",
        "call_id": "call_wait_delta",
        "delta": "",
    })
    assert service.should_suppress_assistant_audio() is True
    service.clear_assistant_audio_suppression()
    assert service.should_suppress_assistant_audio() is False


def test_booking_tool_descriptions_require_exact_booking_confirmation(monkeypatch):
    monkeypatch.setattr(openai_service_module, "is_booking_enabled", lambda: True)
    tools = OpenAISessionManager._realtime_tools()
    by_name = {tool["name"]: tool for tool in tools if tool.get("type") == "function"}

    assert "clarify which exact booking they mean" in by_name["list_my_bookings"]["description"]
    assert "booking_hint" in by_name["list_my_bookings"]["parameters"]["properties"]
    assert "rank likely matches" in by_name["list_my_bookings"]["description"]
    assert "Only call this after the caller has confirmed the exact booking" in by_name["delete_booking"]["description"]
    assert "Only call this after the caller has confirmed the exact booking" in by_name["edit_booking"]["description"]
    assert "Do not say the appointment is booked until this tool succeeds" in by_name["book_appointment"]["description"]
    assert "Do not say the appointment was cancelled until this tool succeeds" in by_name["delete_booking"]["description"]
    assert "Do not say the appointment was rescheduled until this tool succeeds" in by_name["edit_booking"]["description"]


def test_book_appointment_writes_on_first_call_without_confirm_flag(monkeypatch):
    """Single book_appointment call should write to calendar (no server-side staging)."""
    service = OpenAIService()
    connection_manager = _DummyConnectionManager(call_sid=None)
    calls = []

    def _fake_booking_book_appointment(**kwargs):
        calls.append(kwargs)
        return {
            "success": True,
            "message": "Booked OK",
            "event_id": "evt_1",
            "confirmed_slot": {"display": "Fri Apr 17 at 08:00 AM"},
            "calendar_event_link": None,
        }

    monkeypatch.setattr(openai_service_module, "booking_book_appointment", _fake_booking_book_appointment)
    monkeypatch.setattr(openai_service_module.asyncio, "get_event_loop", lambda: _ImmediateExecutorLoop())

    tool_call = {
        "name": "book_appointment",
        "arguments": {
            "slot_start_iso": "2026-04-17T15:00:00Z",
            "contact_name": "Zeb",
            "contact_phone": "218-595-3061",
            "summary": "Deep cleaning appointment",
        },
        "call_id": "call_book_stage_1",
    }

    async def _run():
        assert await service.maybe_handle_tool_call(connection_manager, tool_call) is True

    asyncio.run(_run())
    assert len(calls) == 1
    assert calls[0]["slot_start_iso"] == "2026-04-17T15:00:00Z"
    assert connection_manager.state.appointment_booked is True
    outputs = _tool_outputs(connection_manager)
    assert any("Booked OK" in o for o in outputs)


def test_book_appointment_optional_confirm_exact_slot_still_writes(monkeypatch):
    """confirm_exact_slot is optional; server performs write on first valid call."""
    service = OpenAIService()
    connection_manager = _DummyConnectionManager(call_sid=None)
    calls = []

    def _fake_booking_book_appointment(**kwargs):
        calls.append(kwargs)
        return {
            "success": True,
            "message": "Booked OK",
            "event_id": "evt_2",
            "confirmed_slot": {"display": "Fri Apr 17 at 08:00 AM"},
            "calendar_event_link": None,
        }

    monkeypatch.setattr(openai_service_module, "booking_book_appointment", _fake_booking_book_appointment)
    monkeypatch.setattr(openai_service_module.asyncio, "get_event_loop", lambda: _ImmediateExecutorLoop())

    tool_call = {
        "name": "book_appointment",
        "arguments": {
            "slot_start_iso": "2026-04-17T15:00:00Z",
            "contact_name": "Zeb",
            "contact_phone": "218-595-3061",
            "summary": "Deep cleaning appointment",
            "confirm_exact_slot": True,
        },
        "call_id": "call_book_commit_2",
    }

    async def _run():
        assert await service.maybe_handle_tool_call(connection_manager, tool_call) is True

    asyncio.run(_run())
    assert len(calls) == 1
    assert calls[0]["slot_start_iso"] == "2026-04-17T15:00:00Z"
    assert connection_manager.state.appointment_booked is True
    outputs = _tool_outputs(connection_manager)
    assert any("Booked OK" in o for o in outputs)


def test_book_appointment_second_call_writes_again(monkeypatch):
    """Two distinct tool calls with different call_id each invoke calendar (no idempotent booking gate)."""
    service = OpenAIService()
    connection_manager = _DummyConnectionManager(call_sid=None)
    calls = []

    def _fake_booking_book_appointment(**kwargs):
        calls.append(kwargs)
        return {
            "success": True,
            "message": "Booked OK",
            "event_id": "evt_confirm_first",
            "confirmed_slot": {"display": "Fri Apr 17 at 08:00 AM"},
            "calendar_event_link": None,
        }

    monkeypatch.setattr(openai_service_module, "booking_book_appointment", _fake_booking_book_appointment)
    monkeypatch.setattr(openai_service_module.asyncio, "get_event_loop", lambda: _ImmediateExecutorLoop())

    first = {
        "name": "book_appointment",
        "arguments": {
            "slot_start_iso": "2026-04-17T15:00:00Z",
            "contact_name": "Zeb",
            "contact_phone": "218-595-3061",
            "summary": "Deep cleaning appointment",
            "confirm_exact_slot": True,
        },
        "call_id": "call_book_confirm_first_1",
    }
    second = {
        "name": "book_appointment",
        "arguments": {
            "slot_start_iso": "2026-04-17T15:00:00Z",
            "contact_name": "Zeb",
            "contact_phone": "218-595-3061",
            "summary": "Deep cleaning appointment",
            "confirm_exact_slot": True,
        },
        "call_id": "call_book_confirm_first_2",
    }

    async def _run():
        assert await service.maybe_handle_tool_call(connection_manager, first) is True
        assert await service.maybe_handle_tool_call(connection_manager, second) is True

    asyncio.run(_run())
    assert len(calls) == 2


def test_book_appointment_success_syncs_business_record(monkeypatch):
    """Booking success should sync the business-record lead with event_id and booking fields."""
    service = OpenAIService()
    connection_manager = _DummyConnectionManager(call_sid="CA_book_sync")
    sync_calls = []

    def _fake_booking_book_appointment(**kwargs):
        return {
            "success": True,
            "message": "Booked OK",
            "event_id": "evt_book_sync",
            "confirmed_slot": {"display": "Fri Apr 17 at 08:00 AM"},
            "calendar_event_link": "https://calendar.google.com/event?eid=evt_book_sync",
        }

    async def _fake_sync_call_record_after_booking_action_async(**kwargs):
        sync_calls.append(kwargs)
        return "lead_business_1"

    monkeypatch.setattr(openai_service_module, "booking_book_appointment", _fake_booking_book_appointment)
    monkeypatch.setattr(openai_service_module.asyncio, "get_event_loop", lambda: _ImmediateExecutorLoop())
    monkeypatch.setattr(
        call_records_service_module,
        "sync_call_record_after_booking_action_async",
        _fake_sync_call_record_after_booking_action_async,
    )

    tool_call = {
        "name": "book_appointment",
        "arguments": {
            "slot_start_iso": "2026-04-17T15:00:00Z",
            "contact_name": "Zeb",
            "contact_phone": "218-595-3061",
            "summary": "Deep cleaning appointment",
        },
        "call_id": "call_book_business_sync",
    }

    async def _run():
        assert await service.maybe_handle_tool_call(connection_manager, tool_call) is True
        await _drain_background_tasks()

    asyncio.run(_run())
    assert sync_calls == [
        {
            "action": "booked",
            "call_sid": "CA_book_sync",
            "event_id": "evt_book_sync",
            "contact_phone": "218-595-3061",
            "confirmed_slot": {"display": "Fri Apr 17 at 08:00 AM"},
            "calendar_event_link": "https://calendar.google.com/event?eid=evt_book_sync",
            "appointment_summary": "Deep cleaning appointment",
        }
    ]


def test_delete_booking_success_clears_stored_booking_fields(monkeypatch):
    """Successful cancellation should sync the resolved business-record lead and remember its id."""
    service = OpenAIService()
    connection_manager = _DummyConnectionManager(call_sid="CA_delete_sync")
    sync_calls = []

    def _fake_booking_delete_booking(**kwargs):
        return {"success": True, "message": "Appointment cancelled."}

    async def _fake_sync_call_record_after_booking_action_async(**kwargs):
        sync_calls.append(kwargs)
        return "lead_cancelled_1"

    monkeypatch.setattr(openai_service_module, "booking_delete_booking", _fake_booking_delete_booking)
    monkeypatch.setattr(openai_service_module.asyncio, "get_event_loop", lambda: _ImmediateExecutorLoop())
    monkeypatch.setattr(
        call_records_service_module,
        "sync_call_record_after_booking_action_async",
        _fake_sync_call_record_after_booking_action_async,
    )

    tool_call = {
        "name": "delete_booking",
        "arguments": {
            "event_id": "evt_delete_1",
            "contact_phone": "+12185953061",
            "contact_name": "Emil",
        },
        "call_id": "call_delete_sync_1",
    }

    async def _run():
        assert await service.maybe_handle_tool_call(connection_manager, tool_call) is True
        await _drain_background_tasks()

    asyncio.run(_run())
    assert sync_calls == [
        {
            "action": "cancelled",
            "call_sid": "CA_delete_sync",
            "event_id": "evt_delete_1",
            "contact_phone": "+12185953061",
            "confirmed_slot": None,
            "calendar_event_link": None,
            "note": "Caller requested cancellation of appointment. Confirmed cancelled.",
        }
    ]
    assert connection_manager.state.resolved_business_lead_id == "lead_cancelled_1"
    outputs = _tool_outputs(connection_manager)
    assert any("Appointment cancelled." in o for o in outputs)


def test_edit_booking_success_updates_stored_confirmed_slot(monkeypatch):
    """Successful reschedule should sync the resolved business-record lead and remember its id."""
    service = OpenAIService()
    connection_manager = _DummyConnectionManager(call_sid="CA_edit_sync")
    sync_calls = []
    confirmed_slot = {
        "start": "2026-04-21T18:00:00Z",
        "end": "2026-04-21T19:00:00Z",
        "display": "Tue Apr 21 at 01:00 PM",
    }

    def _fake_booking_edit_booking(**kwargs):
        return {
            "success": True,
            "message": "Appointment rescheduled.",
            "confirmed_slot": confirmed_slot,
        }

    async def _fake_sync_call_record_after_booking_action_async(**kwargs):
        sync_calls.append(kwargs)
        return "lead_rescheduled_1"

    monkeypatch.setattr(openai_service_module, "booking_edit_booking", _fake_booking_edit_booking)
    monkeypatch.setattr(openai_service_module.asyncio, "get_event_loop", lambda: _ImmediateExecutorLoop())
    monkeypatch.setattr(
        call_records_service_module,
        "sync_call_record_after_booking_action_async",
        _fake_sync_call_record_after_booking_action_async,
    )

    tool_call = {
        "name": "edit_booking",
        "arguments": {
            "event_id": "evt_edit_1",
            "new_slot_start_iso": "2026-04-21T18:00:00Z",
            "contact_phone": "+12185953061",
            "contact_name": "Emil",
        },
        "call_id": "call_edit_sync_1",
    }

    async def _run():
        assert await service.maybe_handle_tool_call(connection_manager, tool_call) is True
        await _drain_background_tasks()

    asyncio.run(_run())
    assert sync_calls == [
        {
            "action": "rescheduled",
            "call_sid": "CA_edit_sync",
            "event_id": "evt_edit_1",
            "contact_phone": "+12185953061",
            "confirmed_slot": confirmed_slot,
            "note": "Caller requested reschedule. Appointment moved to Tue Apr 21 at 01:00 PM.",
        }
    ]
    assert connection_manager.state.resolved_business_lead_id == "lead_rescheduled_1"
    assert connection_manager.state.appointment_booked is True
    assert connection_manager.state.confirmed_slot_display == "Tue Apr 21 at 01:00 PM"
    outputs = _tool_outputs(connection_manager)
    assert any("Appointment rescheduled." in o for o in outputs)


def test_submit_lead_updates_resolved_business_row_instead_of_inserting(monkeypatch):
    """When a booking-management call already resolved a business row, submit_lead should update it."""
    service = OpenAIService()
    connection_manager = _DummyConnectionManager(call_sid="CA_followup_sync")
    connection_manager.state.caller_phone_number = "+12185953061"
    connection_manager.state.resolved_call_record_id = "lead_business_42"
    connection_manager.state.resolved_business_lead_id = "lead_business_42"
    deliver_calls = []
    update_calls = []

    async def _fake_save_call_record_async(payload):
        deliver_calls.append(payload)
        return True

    async def _fake_update_existing_call_record_from_payload_async(lead_id, payload):
        update_calls.append((lead_id, payload))
        return True

    monkeypatch.setattr(Config, "CALL_RECORD_BACKEND", "supabase")
    monkeypatch.setattr(
        call_records_service_module,
        "save_call_record_async",
        _fake_save_call_record_async,
    )
    monkeypatch.setattr(
        call_records_service_module,
        "update_existing_call_record_from_payload_async",
        _fake_update_existing_call_record_from_payload_async,
    )

    tool_call = {
        "name": "submit_lead",
        "arguments": {
            "contact_name": "Emil",
            "contact_phone": "caller's number",
            "issue_summary": "Cancellation request",
            "priority": "routine",
            "call_summary": "Caller requested cancellation of the 1 PM appointment. Confirmed cancelled.",
        },
        "call_id": "call_submit_followup_business",
    }

    async def _run():
        assert await service.maybe_handle_tool_call(connection_manager, tool_call) is True
        await _drain_background_tasks()

    asyncio.run(_run())
    assert deliver_calls == []
    assert len(update_calls) == 1
    assert update_calls[0][0] == "lead_business_42"
    assert update_calls[0][1]["call_sid"] == "CA_followup_sync"
    assert update_calls[0][1]["contact"]["phone"] == "+12185953061"
    assert connection_manager.state.lead_submitted is True
    outputs = _tool_outputs(connection_manager)
    assert any("Call record updated with the latest call details." in o for o in outputs)
