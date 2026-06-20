"""
Main system-instructions helpers for the voice-agent starter.

This module owns prompt loading/rendering plus the first greeting and farewell
phrases. Tool schemas and tool execution stay in services.openai_service.
"""
from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_SYSTEM_INSTRUCTIONS_PATH = PROJECT_ROOT / "prompts" / "main_system_instructions.md"

REQUIRED_PROMPT_PLACEHOLDERS = (
    "{agent_name}",
    "{company_name}",
    "{language_instruction}",
    "{accent_instruction}",
    "{reasoning_effort_instruction}",
    "{tools_availability_instruction}",
    "{call_record_instruction}",
    "{booking_instruction}",
    "{transfer_instruction}",
)


def _resolve_instruction_path(raw_path: str | None) -> Path:
    value = (raw_path or "").strip()
    if not value:
        return DEFAULT_SYSTEM_INSTRUCTIONS_PATH
    path = Path(value).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def load_system_instructions(raw_path: str | None = None) -> str:
    path = _resolve_instruction_path(raw_path)
    if path.exists() and path.is_file():
        return path.read_text(encoding="utf-8").strip()
    raise FileNotFoundError(
        f"System instructions file not found: {path}. "
        "Restore prompts/main_system_instructions.md or set SYSTEM_INSTRUCTIONS_PATH."
    )


def get_agent_name(default: str = "") -> str:
    return (os.getenv("AGENT_NAME") or default or "").strip()


def get_greeting_instruction(company_name: str | None = None, agent_name: str | None = None) -> str:
    company = company_name or os.getenv("COMPANY_NAME", "our team")
    name = (agent_name or get_agent_name() or "the voice agent").strip()
    env_welcome = (os.getenv("WELCOME_MESSAGE") or os.getenv("GREETING") or "").strip()
    if env_welcome:
        return (
            env_welcome.replace("{company_name}", company)
            .replace("{agent_name}", name)
        )
    return f"You've reached {company}. I'm {name}. How can I help?"


def get_farewell_instruction(
    company_name: str | None = None,
    reason: str | None = None,
    *,
    call_record_saved: bool = False,
    lead_submitted: bool | None = None,
    appointment_booked: bool = False,
    confirmed_slot_display: str | None = None,
    priority: str | None = None,
) -> str:
    company = company_name or os.getenv("COMPANY_NAME", "our team")
    if appointment_booked and confirmed_slot_display:
        return (
            f"Say a brief goodbye and confirm the appointment: {confirmed_slot_display}. "
            f"Thank them for calling {company}. One short sentence. Do not call any tools; speak now."
        )
    if (priority or "").strip().lower() in {"emergency", "high", "urgent"}:
        return (
            f"Acknowledge the request is time-sensitive and that {company} will prioritize follow-up. "
            "Say a brief goodbye. Do not call any tools; speak now."
        )
    if call_record_saved or bool(lead_submitted):
        return (
            f"Say a brief goodbye on behalf of {company}. Say the call details have been saved for follow-up. "
            "One short sentence. Do not call any tools; speak now."
        )
    base = (
        f"Please deliver a brief, polite goodbye on behalf of {company}. "
        "Keep it to one short sentence. Do not call any tools; speak the goodbye now."
    )
    if isinstance(reason, str) and reason.strip():
        return base + " Acknowledge that the caller requested to end the call."
    return base


def render_system_instructions(
    *,
    company_name: str,
    agent_name: str,
    language_instruction: str,
    accent_instruction: str,
    reasoning_effort_instruction: str = "",
    tools_availability_instruction: str = "",
    call_record_instruction: str,
    booking_instruction: str,
    transfer_instruction: str,
    tools_text: str = "",
    instructions_path: str | None = None,
) -> str:
    template = load_system_instructions(instructions_path)
    replacements = {
        "{company_name}": company_name,
        "{agent_name}": agent_name or "the voice agent",
        "{language_instruction}": language_instruction.strip(),
        "{accent_instruction}": accent_instruction.strip(),
        "{reasoning_effort_instruction}": reasoning_effort_instruction.strip(),
        "{tools_availability_instruction}": tools_availability_instruction.strip(),
        "{call_record_instruction}": call_record_instruction.strip(),
        "{booking_instruction}": booking_instruction.strip(),
        "{transfer_instruction}": transfer_instruction.strip(),
        "{tools_text}": tools_text.strip(),
    }
    rendered = template
    for key, value in replacements.items():
        rendered = rendered.replace(key, value)
    return rendered.strip() + "\n"
