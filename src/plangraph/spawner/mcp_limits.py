"""Compact MCP views and a transport-safe reply size guard."""
import json

MAX_REPLY_BYTES = 8 * 1024 * 1024
PS_ENDED_LIMIT = 20
PS_FIELDS = (
    'run_id', 'status', 'role', 'host', 'pid', 'started', 'ended',
    'last_activity_ts', 'exit_code', 'item_key', 'parent_session',
    'served_model', 'cwd',
)


def ps_reply(runs):
    """Keep all live runs and the newest ended runs, without record payloads."""
    live = [run for run in runs if run.get('status') in ('running', 'queued', 'unknown')]
    ended = [run for run in runs if run.get('status') not in ('running', 'queued', 'unknown')]
    selected = live + ended[:PS_ENDED_LIMIT]
    compact = []
    for run in selected:
        row = {key: run[key] for key in PS_FIELDS if key in run}
        if run.get('status') == 'unknown':
            row['reason'] = 'host query failed'
        leg = run.get('chosen_leg')
        if isinstance(leg, dict) and 'id' in leg:
            row['leg'] = leg['id']
        compact.append(row)
    return {'runs': compact, 'total': len(runs), 'omitted': len(runs) - len(compact)}


def encode_reply(reply):
    """Encode one message, replacing oversized payloads with an RPC error."""
    encoded = json.dumps(reply)
    size = len(encoded.encode('utf-8'))
    if size > MAX_REPLY_BYTES:
        return json.dumps({'jsonrpc': '2.0', 'id': reply.get('id'), 'error': {
            'code': -32000,
            'message': f'ReplyTooLarge: reply size {size} bytes exceeds cap {MAX_REPLY_BYTES} bytes',
            'data': {'type': 'ReplyTooLarge', 'exit_code': 1}}})
    return encoded
