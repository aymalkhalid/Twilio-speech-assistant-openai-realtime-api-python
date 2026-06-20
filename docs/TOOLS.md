# Realtime Tools

Built-in tools are registered in `services/openai_service.py` and listed dynamically in the prompt via `{tools_availability_instruction}`.

## Always available

| Tool | Purpose |
| --- | --- |
| `wait_for_user` | End turn silently on silence, hold music, or non-addressed speech |
| `end_call` | Hang up when the caller explicitly ends the conversation |

## Conditional tools

| Tool | When available |
| --- | --- |
| `save_call_record` | Call-record backend configured |
| `request_human_handoff` | Live transfer configured |
| `get_availability` | Booking enabled + Google Calendar configured |
| `book_appointment` | Booking enabled + Google Calendar configured |
| `list_my_bookings` | Booking enabled + Google Calendar configured |
| `edit_booking` | Booking enabled + Google Calendar configured |
| `delete_booking` | Booking enabled + Google Calendar configured |

`submit_lead` remains accepted inside the compatibility adapter only as a legacy alias. It is not advertised in the generic starter prompt or tool list.

## Prompting conventions

Slow or external tools include **preamble sample phrases** in their descriptions (`get_availability`, booking tools, `save_call_record`, `request_human_handoff`).

| Tool type | Expected behavior |
| --- | --- |
| Read-only lookup | Call when intent and required fields are clear |
| Read-only with phone identity | Confirm callback number digit by digit when not from caller context |
| Write / external | Summarize action, get confirmation, then call with a short preamble |
| Validation failure | Structured JSON: `{"success": false, "message": "...", "next_step": "..."}` |

Exact values matter for booking and records: use ISO slot times from `get_availability`, confirm phone/email before writes, and recover from failures without repeating identical failed calls.

Future external tools should register through `services/tool_registry.py`.
