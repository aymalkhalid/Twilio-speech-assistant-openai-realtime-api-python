# Voice Agent Starter Docs

Docs for the Twilio + OpenAI Realtime starter. Agent behavior is defined in `prompts/main_system_instructions.md`, rendered with env-driven placeholders from `config.py`, and aligned with OpenAI's Realtime prompting guide.

## Guides

- [Architecture](./ARCHITECTURE.md)
- [Configuration](./CONFIGURATION.md) — prompt placeholders, language/accent, reasoning effort
- [Realtime Tools](./TOOLS.md)
- [Cloud Run Deploy](./DEPLOY_CLOUD_RUN.md)

## Prompting reference

- [OpenAI Realtime prompting guide (local copy)](./references/openai-realtime-models-prompting.md)
- [Starter prompt ↔ guide mapping](./references/STARTER_PROMPT_MAPPING.md)

## Database

Supabase SQL helpers live in `docs/supabase-schema/`; start with `call_records_schema.sql`.
