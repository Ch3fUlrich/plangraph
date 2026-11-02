"""Render Claude Code configuration for supervised MCP servers."""

import json
from typing import Any, Dict

from .config import SuperviseConfig


def render_entries(config: SuperviseConfig) -> Dict[str, Dict[str, str]]:
    """Produce the dictionary of Claude Code mcpServers entries."""
    entries: Dict[str, Dict[str, str]] = {}
    for server in config.servers:
        entries[server.name] = {
            "type": "http",
            "url": f"http://{server.host}:{server.port}/mcp",
        }
    return entries


def render_claude_config(config: SuperviseConfig) -> Dict[str, Any]:
    """Produce a full Claude Code configuration object wrapping mcpServers."""
    return {"mcpServers": render_entries(config)}


def render_json(config: SuperviseConfig, wrap: bool = False, indent: int = 2) -> str:
    """Format the rendered configuration as a JSON string."""
    data = render_claude_config(config) if wrap else render_entries(config)
    return json.dumps(data, indent=indent)
