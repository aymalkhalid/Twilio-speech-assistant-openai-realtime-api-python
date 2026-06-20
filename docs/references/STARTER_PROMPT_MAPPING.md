# Starter prompt mapping

This maps the starter's main system prompt to sections in [openai-realtime-models-prompting.md](./openai-realtime-models-prompting.md).

**Live prompt:** `prompts/main_system_instructions.md`  
**Tool schemas:** `services/openai_service.py`  
**Runtime config:** `config.py`, `.env`

## Current coverage

| Guide section | Starter prompt section | Notes |
| --- | --- | --- |
| Role and Objective | `# Role and Objective` | Generic business voice agent scope |
| Conversation Flow | `# Conversation Flow` | Lightweight intake; not a full state machine |
| Reasoning | `# Reasoning`, `{reasoning_effort_instruction}` | OpenAI when/when-not rules; API effort injected for gpt-realtime-2 |
| Personality and Tone | `# Personality and Tone` | Short phone-friendly style |
| Language | `{language_instruction}` | English-primary by default; optional multilingual via `LANGUAGE_SWITCH_POLICY` |
| Accent | `{accent_instruction}` | English delivery with configurable accent; separate from language |
| Preambles | `# Preambles` | OpenAI-aligned when/when-not/style; mapped to slow tools |
| Verbosity | `# Verbosity` | Task-type length rules with booking comparison example |
| Handling silence | `# Handling Silence and Background Noise` | Uses `wait_for_user` per OpenAI pattern |
| Entity Capture | `# Entity Capture` + collection order, spelled-out chars, spoken numbers, confirmation, workflow | Full OpenAI exact-entity pattern for phone, email, slots |
| Instruction precision | `# Instruction Precision` | Avoid literal traps; scoped rules over broad must/always |
| Tools | `# Tools`, `{tools_availability_instruction}`, dynamic blocks | Eagerness, read/write rules, failure recovery |
| Escalation / Safety | `# Safety`, `# End Call` | Basic guardrails; expand for production |

## Implemented in code (not only prompt)

| Guide recommendation | Where in starter |
| --- | --- |
| `wait_for_user` no-op tool | `services/openai_service.py` |
| Tool availability sync | Tools registered per session in `openai_service.py`; prompt uses `{booking_instruction}` etc. |
| Preamble sample phrases on slow tools | `services/openai_service.py` tool descriptions |
| Structured tool validation failures | `OpenAIService._format_tool_failure_output()` JSON envelope |
| Write-action confirmation | Prompt: confirm before side-effect tools |
| Reasoning effort | `REALTIME_REASONING_EFFORT` in `config.py`; sent in session for `gpt-realtime-2`; mirrored in prompt |

## Gaps to consider when extending

These guide sections are not fully represented in the starter yet. Add them when a feature needs them:

- **Long Context Behavior** — structured session context for long calls
- **Message Channels** — commentary vs final_answer handling in app code
- **Conversation state machine** — JSON states or `session.update` per phase
- **Safety & Escalation thresholds** — explicit escalation counts and phrases

## Prompt audit helpers

From the Realtime 1.5 section of the guide, use these meta-prompts when iterating:

1. **Instructions Quality Prompt** — find ambiguity, conflicts, and missing definitions
2. **Prompt Optimization Meta Prompt** — tighten a specific failure mode

Both are copied in [openai-realtime-models-prompting.md](./openai-realtime-models-prompting.md) under **Instructions → Instruction Following**.

## Workflow

1. Identify the behavior gap or new feature.
2. Find the matching section in the [OpenAI guide](./openai-realtime-models-prompting.md).
3. Update `prompts/main_system_instructions.md` first.
4. Update `config.py` builders or `.env` when language, accent, reasoning effort, or feature toggles are involved.
5. Update `services/openai_service.py` if tools or side effects change.
6. Update [docs/CONFIGURATION.md](../CONFIGURATION.md) when placeholders or env vars change.
7. Run `pytest tests/test_system_instructions.py` after prompt or config builder changes.
