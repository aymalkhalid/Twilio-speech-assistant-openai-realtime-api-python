# Voice Agent Starter - Twilio + OpenAI Realtime

A clean Python/FastAPI starter for phone-based voice agents using Twilio Voice
Media Streams and OpenAI Realtime.

The starter is intentionally generic: one editable system-instructions file,
tool definitions in code, optional integrations through environment variables,
and no legacy industry/profile YAML layer.

## What It Includes

- Twilio Media Streams to OpenAI Realtime audio bridge
- Realtime voice selection, VAD, interruption handling, and session renewal
- Silence/background-audio handling via `wait_for_user` (OpenAI Realtime pattern)
- Editable main prompt at `prompts/main_system_instructions.md` (OpenAI Realtime 2-aligned)
- `wait_for_user`, `save_call_record`, booking, transfer, and end-call tool handling
- Optional Supabase-backed dashboard for call records
- Optional call recording, playback proxy, and transcript generation
- Optional Google Calendar booking tools
- Optional outbound calling campaigns
- Disabled-by-default MCP/tool adapter scaffold
- Google Cloud Run deployment script

## Quick Start

```bash
python3 -m venv env
source env/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Set `OPENAI_API_KEY` in `.env`, then run:

```bash
python main.py
```

Point your Twilio Voice webhook to:

```text
https://YOUR_HOST/incoming-call
```

## Customize The Agent

**Prompt (behavior):** edit `prompts/main_system_instructions.md`  
**Tools (schemas + side effects):** edit `services/openai_service.py`  
**Language/accent/reasoning:** set env vars below (rendered by `config.py`)

Prompting follows the [OpenAI Realtime guide](./docs/references/openai-realtime-models-prompting.md). See [starter mapping](./docs/references/STARTER_PROMPT_MAPPING.md) for section coverage.

Useful env values:

```env
COMPANY_NAME=Acme Voice Agent Demo
AGENT_NAME=Alex
OPENAI_REALTIME_MODEL=gpt-realtime-2
REALTIME_REASONING_EFFORT=low
VOICE=cedar
ASSISTANT_LANGUAGE=English
ASSISTANT_ACCENT=neutral American
ASSISTANT_ACCENT_STRENGTH=light
LANGUAGE_SWITCH_POLICY=default_only
```

Full configuration: [docs/CONFIGURATION.md](./docs/CONFIGURATION.md)

## Storage And Dashboard

The core phone agent runs without Supabase. Set `CALL_RECORD_BACKEND=supabase` and
Supabase credentials to enable `/dashboard`, `/calls`, recordings, transcripts,
notes, and statuses.

The default table is `call_records`. Older `leads` tables remain supported by setting
`SUPABASE_CALL_RECORD_TABLE=leads` during migration.

## Deploy To Cloud Run

```bash
./scripts/deploy-cloudrun.sh
```

The script reads local `.env`, excludes local secrets, and deploys the container.
