"""Tool registry scaffold for future external tools.

The starter currently exposes built-in OpenAI Realtime tools directly from
services.openai_service. This registry is intentionally small: future adapters
such as MCP can register JSON schemas and async handlers here without changing
the Twilio/OpenAI audio bridge.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable


ToolHandler = Callable[[dict[str, Any], Any], Awaitable[str]]


@dataclass(frozen=True)
class RegisteredTool:
    name: str
    schema: dict[str, Any]
    handler: ToolHandler | None = None
    enabled: bool = True


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(self, tool: RegisteredTool) -> None:
        if not tool.name:
            raise ValueError("Tool name is required")
        self._tools[tool.name] = tool

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.schema for tool in self._tools.values() if tool.enabled]

    def get(self, name: str) -> RegisteredTool | None:
        tool = self._tools.get(name)
        return tool if tool and tool.enabled else None


external_tool_registry = ToolRegistry()
