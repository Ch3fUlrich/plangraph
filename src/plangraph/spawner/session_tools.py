"""MCP session tool schemas and fail-closed argument checks.

SESSION-SPAWN-MCP slice 4/4 (the 400-line file gate, D-3209/D-3215):
``SESSION_TOOLS`` (the list plus the additionalProperties loop) and
``_session_arg_errors`` moved unchanged out of ``mcp.py`` so each file
keeps one responsibility. ``mcp.py`` imports both and still extends
``TOOLS_METADATA`` with ``SESSION_TOOLS`` at the same place; tests take
these names from ``mcp`` and get them through the import (D-3193 point
6: refuse undeclared or wrong-typed arguments at the MCP boundary).
"""
from __future__ import annotations

# SESSION-SPAWN-MCP: Claude Code session lifecycle (docs/spawner-sessions.md).
SESSION_TOOLS = [
    {'name': 'plangraph_session_launch', 'description': 'Launch a background Claude Code session',
     'inputSchema': {'type': 'object', 'properties': {
         'name': {'type': 'string'}, 'cwd': {'type': 'string'}, 'model': {'type': 'string'},
         'effort': {'type': 'string'}, 'prompt': {'type': 'string'}, 'prompt_file': {'type': 'string'},
         'mcp_config': {'type': 'string'}, 'handoff': {'type': 'string'}, 'parent': {'type': 'string'},
         'permission_mode': {'type': 'string'}, 'dry_run': {'type': 'boolean'}},
         'required': ['name', 'cwd', 'model', 'effort', 'mcp_config', 'permission_mode']}},
    {'name': 'plangraph_session_stop', 'description': 'Stop a Claude Code session by name or id',
     'inputSchema': {'type': 'object', 'properties': {
         'name': {'type': 'string'}, 'id': {'type': 'string'}}}},
    {'name': 'plangraph_session_relaunch', 'description': 'Relaunch a stopped session from its handoff',
     'inputSchema': {'type': 'object', 'properties': {
         'name': {'type': 'string'}, 'handoff': {'type': 'string'}},
         'required': ['name']}},
    {'name': 'plangraph_session_mark_ready', 'description': 'Mark a session ready with handoff and card',
     'inputSchema': {'type': 'object', 'properties': {
         'name': {'type': 'string'}, 'handoff': {'type': 'string'}, 'card': {'type': 'string'}},
         'required': ['name', 'handoff']}},
    {'name': 'plangraph_session_list', 'description': 'List sessions with context usage',
     'inputSchema': {'type': 'object', 'properties': {}}},
]
for _t in SESSION_TOOLS:
    # Undeclared argument keys are refused at the MCP boundary
    # (_session_arg_errors); the schema says so declaratively too (R-25).
    _t['inputSchema']['additionalProperties'] = False


def _session_arg_errors(schema, args):
    """First MCP boundary error for one session tool's arguments, or None
    (D-3193 point 6). `schema` is the tool's SESSION_TOOLS entry. JSON types
    are matched strictly - bool is not an integer, and a truthy string is not
    a boolean - so a wrong-typed value is refused before dispatch instead of
    being coerced (dry_run="yes")."""
    if not isinstance(args, dict):
        return "arguments must be an object"
    input_schema = schema.get('inputSchema', {})
    props = input_schema.get('properties', {})
    undeclared = sorted(set(args) - set(props))
    if undeclared:
        return f"undeclared session arguments for {schema['name']}: {', '.join(undeclared)}"
    for key in input_schema.get('required', ()):
        if key not in args:
            return f"missing required argument: {key}"
    json_types = {'string': str, 'boolean': bool, 'object': dict}
    for key, value in args.items():
        expected = props.get(key, {}).get('type')
        if expected == 'integer':
            ok = isinstance(value, int) and not isinstance(value, bool)
        elif expected in json_types:
            ok = isinstance(value, json_types[expected])
        else:
            ok = True  # untyped property: nothing to compare against
        if not ok:
            return f"argument {key} must be {expected}"
    return None
