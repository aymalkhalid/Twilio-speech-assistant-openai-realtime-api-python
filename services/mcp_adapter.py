"""Disabled-by-default MCP adapter scaffold.

This file documents the intended integration point for MCP without adding a
runtime dependency or connecting to a real MCP server in v1.

Future implementation shape:
  1. Load allowed MCP servers/tools from config.
  2. Convert allowed MCP tool schemas to OpenAI Realtime function schemas.
  3. Register those schemas in services.tool_registry.external_tool_registry.
  4. Dispatch matching function calls to the MCP client.
  5. Return function_call_output text to the Realtime session.
"""
from __future__ import annotations

from services.tool_registry import ToolRegistry


def load_mcp_tools(registry: ToolRegistry) -> None:
    """No-op placeholder. MCP is intentionally disabled in the starter v1."""
    return None
