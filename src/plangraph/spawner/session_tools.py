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

import asyncio

from plangraph.routing_errors import ParameterRefused

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


async def dispatch_session_tool(server, name, arguments):
    """Dispatch only the five declared session tools (R-7). The MCP
    boundary validates arguments against the tool's schema first
    (_session_arg_errors, D-3193 point 6): undeclared keys, missing
    required keys and wrong JSON types become audited refusals - never a
    KeyError, TypeError or a coerced value (dry_run="yes"). Deeper checks
    stay inside Sessions' _action, so they are audited too (R-25)."""
    schema = {t['name']: t for t in SESSION_TOOLS}.get(name)
    if schema is None:
        raise ValueError(f"unknown session tool: {name}")
    args = {} if arguments is None else arguments
    error = _session_arg_errors(schema, args)
    if error:
        raw_name = args.get('name') if isinstance(args, dict) else None
        try:
            server._sessions().audit_refusal(
                name[len('plangraph_session_'):],
                raw_name if isinstance(raw_name, str) else '', error)
        except OSError:
            pass  # audit is best effort: the refusal below is the outcome
        raise ParameterRefused(error)
    args = dict(args)
    sessions = server._sessions()
    if name == 'plangraph_session_launch':
        return await asyncio.to_thread(sessions.launch,
            name=args.get('name'), cwd=args.get('cwd'), model=args.get('model'),
            effort=args.get('effort'), mcp_config=args.get('mcp_config'),
            permission_mode=args.get('permission_mode'), prompt=args.get('prompt'),
            prompt_file=args.get('prompt_file'), handoff=args.get('handoff'),
            parent=args.get('parent'),
            dry_run=args['dry_run'] if 'dry_run' in args else False)
    if name == 'plangraph_session_stop':
        return await asyncio.to_thread(sessions.stop, name=args.get('name'),
                                       session_id=args.get('id'))
    if name == 'plangraph_session_relaunch':
        return await asyncio.to_thread(sessions.relaunch, args.get('name'),
                                       args.get('handoff'))
    if name == 'plangraph_session_mark_ready':
        return await asyncio.to_thread(sessions.mark_ready, args.get('name'),
                                       args.get('handoff'), args.get('card'))
    if name == 'plangraph_session_list':
        return await asyncio.to_thread(sessions.list)
    raise ValueError(f"unknown session tool: {name}")
