"""Compact MCP views, a transport-safe reply size guard and the source-change guard."""
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import subprocess
import uuid

from plangraph.routing_errors import SpawnRefused

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


def _fingerprint(root):
    """Cheap (path, size, mtime_ns) digest of every .py file under ``root``.

    Any read failure yields a fresh token, so the identity differs on every call
    and real runs are refused (fail closed) instead of ignoring the file."""
    digest = hashlib.sha256()
    try:
        for path in sorted(root.rglob('*.py')):
            info = path.stat()
            digest.update(f'{path.relative_to(root).as_posix()}\0{info.st_size}\0{info.st_mtime_ns}\0'.encode())
    except OSError:
        digest.update(uuid.uuid4().bytes)
    return digest.hexdigest()


def current_source_identity(source=None):
    """Identify the ``plangraph`` package tree real runs import: Git HEAD (when
    available, ignoring cwd and inherited GIT_*) combined with a file fingerprint."""
    source = Path(source) if source is not None else Path(__file__).resolve().parents[1]
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    head = ''
    try:
        out = subprocess.run(['git', '-C', str(source), 'rev-parse', '--verify', 'HEAD'],
                             env=env, capture_output=True, text=True, encoding='utf-8',
                             errors='replace', check=True, timeout=2).stdout.strip()
        if re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', out):
            head = out
    except (OSError, subprocess.SubprocessError):
        pass
    return hashlib.sha256((head + _fingerprint(source)).encode()).hexdigest()[:12]


SPAWN_MODULES = (
    'plangraph.planner.routing', 'plangraph.planner.legs', 'plangraph.planner.seats',
    'plangraph.planner.measured_down', 'plangraph.routing_errors', 'plangraph.settings',
    'plangraph.routing_support.clients', 'plangraph.routing_support.liveness',
    'plangraph.routing_support.leg_state_flag', 'plangraph.routing_support.opencode_render',
    'plangraph.routing_support.host_keys',
    *('plangraph.spawner.' + name for name in (
        'runner', 'service', 'leg_state', 'target', 'private_io', 'opencode_overlay',
        'opencode_isolation', 'gateway_witness', 'opencode_answer', 'resolver', 'probe_cleanup',
        'run_watchdog', 'win_recorded', 'proc_kill', 'env_redact', 'question_bridge',
        'cancel_job', 'hung_capture', 'seat_artifact', 'stall_watchdog', 'fixer', 'inline_diff',
        'context_pack', 'prompt_guard')))
GUARDED_TOOLS = ('plangraph_agent_spawn', 'plangraph_session_launch', 'plangraph_session_relaunch')


class SourceChanged(SpawnRefused):
    """The MCP process must reconnect before launching from a changed source tree."""


class SourceGuard:
    def __init__(self, identity=None):
        for name in SPAWN_MODULES:  # lazy spawn-path imports must not straddle a tree update
            importlib.import_module(name)
        self.identity = identity or current_source_identity
        self.started = self.identity()

    def restart_required(self):
        current = self.identity()
        if current != self.started:
            return f'restart required: {self.started[:12]} -> {current[:12]}, run /mcp reconnect'
        return None

    def check(self, name, arguments):
        """Refuse tools that start a real run or session; dry runs, route, ps, status,
        cancel, probe, session stop/list/mark_ready stay available."""
        if name in GUARDED_TOOLS and not arguments.get('dry_run', False):
            if message := self.restart_required():
                raise SourceChanged(message)
