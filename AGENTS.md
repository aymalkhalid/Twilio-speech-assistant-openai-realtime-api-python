# Agent instructions

This is a clean Twilio + OpenAI Realtime voice-agent starter.

## Source of truth

- Main behavior prompt: `prompts/main_system_instructions.md`
- OpenAI Realtime prompting reference: `docs/references/openai-realtime-models-prompting.md`
- Starter prompt mapping: `docs/references/STARTER_PROMPT_MAPPING.md`
- Runtime env/config: `config.py` and `.env`
- Realtime tool schemas and handlers: `services/openai_service.py`
- Optional external tool scaffold: `services/tool_registry.py`, `services/mcp_adapter.py`
- FastAPI/Twilio entrypoints: `main.py`

## Rules for changes

- Change agent behavior in `prompts/main_system_instructions.md` when possible.
- Change language, accent, reasoning effort, and feature-specific prompt text via `config.py` builders and `.env`.
- Keep tool schemas and side-effect handlers in `services/openai_service.py`.
- Keep booking logic in `services/google_calendar_booking_service.py`.
- Use `services/call_records_service.py` as the app-facing storage facade; keep low-level webhook/Supabase compatibility mapping in `services/webhook_service.py`.
- Add tests when changing prompt rendering, tool registration, booking, dashboard APIs, or storage payloads.
- Do not add industry/profile YAML files back into this starter.
- After prompt changes, check [docs/references/STARTER_PROMPT_MAPPING.md](docs/references/STARTER_PROMPT_MAPPING.md) and run `pytest tests/test_system_instructions.py`.
