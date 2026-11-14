"""Compact MCP views, a transport-safe reply size guard and the source-change guard."""
import ast
from collections.abc import Mapping
import hashlib
import importlib
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

from plangraph.routing_errors import ParameterRefused, SpawnRefused

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
    """Cheap (path, size, mtime_ns) digest of package source and assets.

    Any read failure yields a fresh token, so the identity differs on every call
    and real runs are refused (fail closed) instead of ignoring the file."""
    digest = hashlib.sha256()
    try:
        if not root.is_dir():
            raise OSError('package tree is unavailable')
        for path in sorted(root.rglob('*')):
            if any(part in ('.git', '__pycache__') for part in path.relative_to(root).parts) or path.suffix in ('.pyc', '.pyo'):
                continue
            if path.is_dir():
                continue
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


def _spawn_modules(root=None, entries=('plangraph.spawner.mcp',), sources=None):
    """Follow package imports at every nesting depth before capturing identity.

    Include transitive imports and package initializers, including imports inside
    functions and platform branches. Resolve only local source modules: imported
    attributes and external dependencies are left to Python's normal importer.
    Read/parse failures abort startup rather than silently weakening the guard.
    """
    root = root or Path(__file__).resolve().parents[1]
    pending, found = list(entries), set()
    while pending:
        name = pending.pop()
        if name in found or not (name == 'plangraph' or name.startswith('plangraph.')):
            continue
        path = root.joinpath(*name.split('.')[1:])
        source = path / '__init__.py' if path.is_dir() else path.with_suffix('.py')
        if not source.is_file():
            continue  # An imported attribute, not a source module.
        found.add(name)
        package = name if source.name == '__init__.py' else name.rpartition('.')[0]
        if name != 'plangraph':
            pending.append(name.rpartition('.')[0])
        text = source.read_text(encoding='utf-8')
        if sources is not None:
            sources[name] = (source, text)
        for node in ast.walk(ast.parse(text, filename=str(source))):
            if isinstance(node, ast.Import):
                pending.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                base = importlib.util.resolve_name('.' * node.level + (node.module or ''), package)
                pending.append(base)
                pending.extend(base + '.' + alias.name for alias in node.names if alias.name != '*')
    return tuple(sorted(found))


_SPAWN_SOURCES = {}
SPAWN_MODULES = _spawn_modules(sources=_SPAWN_SOURCES)


class _FrozenLoader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        path, text = _SPAWN_SOURCES[fullname]
        return compile(text, str(path), 'exec', dont_inherit=True)


class _FrozenSpawnImports:
    """Keep lazy execution/platform checks while pinning source at MCP import.

    Eager execution of the whole closure would import optional POSIX keeper code
    on Windows. A source snapshot instead prevents both disk and .pyc reloads
    after a checkout update without executing otherwise unused modules.
    """
    def find_spec(self, fullname, path=None, target=None):
        if fullname in _SPAWN_SOURCES:
            source, _ = _SPAWN_SOURCES[fullname]
            return importlib.util.spec_from_file_location(
                fullname, source, loader=_FrozenLoader(fullname, str(source)))


sys.meta_path.insert(0, _FrozenSpawnImports())
GUARDED_TOOLS = ('plangraph_agent_spawn', 'plangraph_session_launch', 'plangraph_session_relaunch')


class SourceChanged(SpawnRefused):
    """The MCP process must reconnect before launching from a changed source tree."""


class SourceGuard:
    def __init__(self, identity=None):
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
        dry = (name in ('plangraph_agent_spawn', 'plangraph_session_launch')
               and isinstance(arguments, Mapping) and arguments.get('dry_run') is True)
        if name in GUARDED_TOOLS and not dry:
            if message := self.restart_required():
                raise SourceChanged(message)
            if not isinstance(arguments, Mapping):
                raise ParameterRefused('arguments must be an object')
