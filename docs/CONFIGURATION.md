# Configuration

The starter uses environment variables, runtime config in `config.py`, and one editable prompt file.

## Main Prompt

Edit behavior in:

```text
prompts/main_system_instructions.md
```

At runtime, `config.py` renders placeholders and produces `Config.SYSTEM_MESSAGE` for the Realtime session.

### Static prompt sections

The markdown file defines OpenAI Realtime-aligned behavior:

- Role, conversation flow, reasoning, personality
- Preambles and verbosity
- Silence handling (`wait_for_user`) and unclear audio
- Entity capture (collection order, spelled-out values, spoken numbers, confirmation workflow)
- Instruction precision (avoid overly broad `always` / `must` rules)
- Tool eagerness, read/write rules, and failure recovery

### Injected placeholders

| Placeholder | Built by | Purpose |
| --- | --- | --- |
| `{company_name}` | `Config.COMPANY_NAME` | Business name |
| `{agent_name}` | `Config.AGENT_NAME` | Spoken agent name |
| `{language_instruction}` | `_build_language_instruction()` | English-primary or multilingual policy |
| `{accent_instruction}` | `_build_accent_instruction()` | English delivery accent, separate from language |
| `{reasoning_effort_instruction}` | `_build_reasoning_effort_instruction()` | gpt-realtime-2 effort guidance (empty for older models) |
| `{tools_availability_instruction}` | `_build_tools_availability_instruction()` | Lists tools actually enabled this session |
| `{call_record_instruction}` | `_build_call_record_instruction()` | When/how to call `save_call_record` |
| `{booking_instruction}` | `_build_booking_instruction()` | Booking flow and confirmation rules |
| `{transfer_instruction}` | `_build_transfer_instruction()` | Human handoff rules |

Change wording in the markdown file when possible. Change env vars when behavior depends on enabled features or voice/language settings.

Reference: [Starter prompt ↔ guide mapping](./references/STARTER_PROMPT_MAPPING.md)

## Core Env

| Variable | Default | Notes |
| --- | --- | --- |
| `OPENAI_API_KEY` | — | Required |
| `COMPANY_NAME` | `Acme Voice Agent Demo` | Spoken business name |
| `AGENT_NAME` | — | Spoken agent name |
| `AGENT_LABEL` | `generic_voice_agent` | Internal label |
| `SYSTEM_INSTRUCTIONS_PATH` | `prompts/main_system_instructions.md` | Alternate prompt file |
| `OPENAI_REALTIME_MODEL` | `gpt-realtime-2` | Realtime model id |
| `REALTIME_REASONING_EFFORT` | `low` | `minimal` … `xhigh`; sent in session for `gpt-realtime-2` only |
| `VOICE` | `cedar` | OpenAI Realtime voice |
| `TEMPERATURE` | `0.8` | Session temperature |

## Language And Accent

English is the primary language. Accent is configured separately and does not change the response language.

| Variable | Default | Notes |
| --- | --- | --- |
| `ASSISTANT_LANGUAGE` | `English` | Default response language |
| `ASSISTANT_ACCENT` | `neutral American` | e.g. `neutral British`, `neutral Australian` |
| `ASSISTANT_ACCENT_STRENGTH` | `light` | `none`, `light`, `moderate` |
| `LANGUAGE_SWITCH_POLICY` | `default_only` | `default_only` pins English; `explicit_or_substantive` allows multilingual switching |

Example — English with a British accent, no language switching:

```env
ASSISTANT_LANGUAGE=English
ASSISTANT_ACCENT=neutral British
ASSISTANT_ACCENT_STRENGTH=light
LANGUAGE_SWITCH_POLICY=default_only
```

## Optional Features

- **Call records:** `CALL_RECORD_BACKEND` (`webhook`, `supabase`, …), `WEBHOOK_URL`, `SUPABASE_URL`, `SUPABASE_KEY`, `SUPABASE_CALL_RECORD_TABLE=call_records`
- **Booking:** `BOOKING_ENABLED=true`, `GOOGLE_CALENDAR_ID`, `GOOGLE_CALENDAR_CREDENTIALS_JSON`
- **Recording:** `CALL_RECORDING_ENABLED=true`, `RECORDING_STATUS_CALLBACK_BASE_URL`
- **Transfer:** `HUMAN_TRANSFER_URL`, `HUMAN_TRANSFER_ENABLED`, `HUMAN_TRANSFER_DIAL_NUMBER`
- **Outbound:** `OUTBOUND_ENABLED=true`, Twilio credentials, Supabase credentials

## Supabase Schema

Run `docs/supabase-schema/call_records_schema.sql` when enabling Supabase call-record storage. Existing deployments can point `SUPABASE_CALL_RECORD_TABLE` at an older `leads` table while migrating.

## Changing Behavior Safely

1. Edit `prompts/main_system_instructions.md` for conversational rules.
2. Edit `.env` for language, accent, reasoning effort, and feature toggles.
3. Edit tool schemas/handlers in `services/openai_service.py` when tool args or side effects change.
4. Run `pytest tests/test_system_instructions.py` after prompt or config builder changes.
