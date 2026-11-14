"""Bounded, injectable host inspection; unavailable data is never zero drift."""
import ast
import json
import os
import re
import shlex
import subprocess
from pathlib import Path

from .release import git

def transport(host):
    live = host['repos']['plangraph']['live_dir']
    command = ['git', '-C', live, 'rev-parse', 'HEAD']
    if host['transport'] == 'ssh':
        remote = (subprocess.list2cmdline(command) if host['os'] == 'windows'
                  else shlex.join(command))
        command = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=2',
                   '--', host.get('ssh_target') or host['id'], remote]
    elif host['transport'] != 'local':
        raise ValueError('unsupported transport')
    return subprocess.check_output(command, text=True, stderr=subprocess.DEVNULL, timeout=5).strip()


def rows(registry, release, query=transport, count=None):
    count = count or (lambda live, target: int(git('rev-list', '--count', f'{live}..{target}')))
    result = []
    for host in registry['hosts']:
        live, behind = 'unknown', 'unknown'
        try:
            sha = query(host)
            if not re.fullmatch('[0-9a-f]{40,64}', sha):
                raise ValueError('invalid sha')
            live = sha
            if release != 'unknown':
                behind = count(live, release)
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            behind = 'unknown'
        result.append({'host': host['id'], 'live_sha': live, 'release_sha': release, 'behind': behind})
    return result


def repo_git(repo, *args, check=True):
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    env['GIT_TERMINAL_PROMPT'] = '0'
    return subprocess.run(['git', '-C', str(repo), *args], check=check, capture_output=True,
                          text=True, timeout=30, env=env)


def import_closure(repo, revision, *, roots=None):
    """Keeper package plus literal internal imports, including lazy/relative imports."""
    paths = repo_git(repo, 'ls-tree', '-r', '-z', '--name-only', revision).stdout.split('\0')
    modules = {}
    for path in paths:
        if path.endswith('.py'):
            name = (path[4:-3] if path.startswith('src/') else path[:-3]).replace('/', '.')
            modules[name.removesuffix('.__init__')] = path
    pending = [name for name, path in modules.items()
               if (name == 'plangraph.keeper' or name.startswith('plangraph.keeper.'))
               and (roots is None or path in roots)]
    seen = set()
    while pending:
        name = pending.pop()
        if name in seen or name not in modules:
            continue
        seen.add(name)
        path = modules[name]
        source = repo_git(repo, 'show', f'{revision}:{path}').stdout
        package = name if path.endswith('/__init__.py') else name.rpartition('.')[0]
        imports = []
        for node in ast.walk(ast.parse(source, filename=path)):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    parts = package.split('.')
                    base = '.'.join(parts[:len(parts) - node.level + 1])
                    base += ('.' + node.module) if node.module else ''
                else:
                    base = node.module or ''
                imports.append(base)
                imports.extend(base + '.' + alias.name for alias in node.names)
            elif (isinstance(node, ast.Call) and node.args
                    and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)
                    and (isinstance(node.func, ast.Name) and node.func.id in ('__import__', 'import_module')
                         or isinstance(node.func, ast.Attribute) and node.func.attr == 'import_module')):
                imported = node.args[0].value
                if imported.startswith('.'):
                    level = len(imported) - len(imported.lstrip('.'))
                    base = node.args[1].value if len(node.args) > 1 and isinstance(node.args[1], ast.Constant) else package
                    imported = '.'.join(base.split('.')[:len(base.split('.')) - level + 1]) + imported[level - 1:]
                imports.append(imported)
        for imported in [name, *imports]:
            parts = imported.split('.')
            pending.extend('.'.join(parts[:i]) for i in range(1, len(parts) + 1)
                           if '.'.join(parts[:i]) in modules and '.'.join(parts[:i]) not in seen)
    return {modules[name] for name in seen}


def keeper_changes(repo, loaded, target):
    old_paths = set(repo_git(repo, 'ls-tree', '-r', '-z', '--name-only', loaded).stdout.split('\0'))
    watched = import_closure(repo, loaded) | import_closure(repo, target, roots=old_paths)
    changed = repo_git(repo, 'diff', '--no-renames', '-z', '--name-only', loaded, target).stdout.split('\0')
    return sorted(path for path in changed if path in old_paths
                  and (path.startswith('src/plangraph/keeper/') or path in watched))


def pid_start(pid):
    try:
        return (Path('/proc') / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def running_keeper(path):
    try:
        row = json.loads(Path(path).read_text())
    except FileNotFoundError:
        return None
    if not isinstance(row, dict) or type(row.get('pid')) is not int or row['pid'] <= 0:
        raise ValueError('invalid keeper loaded state')
    if os.name == 'nt':
        if keeper_process_pid(platform='nt', match_pid=row['pid']) != row['pid']:
            return None
    else:
        try:
            os.kill(row['pid'], 0)
        except ProcessLookupError:
            return None
        except PermissionError:
            pass  # A process we cannot inspect must not be mistaken for stopped.
    if row.get('pid_start') is not None and pid_start(row['pid']) not in (None, row['pid_start']):
        return None
    sha = row.get('loaded_sha')
    if not isinstance(sha, str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', sha):
        raise ValueError('keeper loaded SHA unavailable')
    return sha


class KeeperProbeError(RuntimeError):
    """Fixed public diagnostics for inconclusive keeper liveness checks."""


def _keeper_launch(executable, command, *, windows=False):
    """Recognize an interpreter's launch target, never an incidental argument."""
    executable = re.split(r'[/\\]', executable)[-1]
    if not re.fullmatch(r'(?:pythonw?|pypy)[0-9.]*t?(?:[.]exe)?', executable, re.IGNORECASE):
        return False
    lexer = shlex.shlex(command, posix=not windows)
    lexer.whitespace_split, lexer.commenters = True, ""
    args = (part.strip('"') for part in lexer)
    next(args, None)
    for arg in args:
        if arg in ('-c', '-'):
            return False
        if arg == '-m':
            return next(args, '') in ('plangraph.keeper.daemon', 'plangraph.keeper')
        if arg in ('-X', '-W'):
            next(args, None)
        elif arg == '--':
            return bool(re.fullmatch(r'(?:.*[/\\])?plangraph[/\\]keeper[/\\]daemon[.]py', next(args, '')))
        elif not arg.startswith('-'):
            return bool(re.fullmatch(r'(?:.*[/\\])?plangraph[/\\]keeper[/\\]daemon[.]py', arg))
    return False


def keeper_process_pid(*, platform=None, match_pid=None):
    """Inspect interpreter launch targets independently of unit ownership."""
    windows = (platform or os.name) == 'nt'
    command = (['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                'Get-CimInstance Win32_Process | Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Compress']
               if windows else ['ps', '-A', '-o', 'pid=', '-o', 'comm=', '-o', 'args='])
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=5)
        if result.returncode:
            raise KeeperProbeError('keeper process query failed')
        if windows:
            rows = json.loads(result.stdout or '[]')
            rows = [rows] if isinstance(rows, dict) else rows
            entries = [(row['ProcessId'], row['Name'], row['CommandLine']) for row in rows]
        else:
            entries = [line.split(None, 2) for line in result.stdout.splitlines() if line.strip()]
        for entry in entries:
            pid, executable, *arguments = entry
            args = arguments[0] if arguments else ''
            pid = int(pid)
            if pid == 0:  # Windows exposes the non-interpreter idle process as PID 0.
                continue
            if pid < 0:
                raise ValueError('invalid PID')
            if match_pid is not None and pid != match_pid:
                continue
            if pid != os.getpid() and _keeper_launch(executable, args or '', windows=windows):
                return pid
        return 0
    except subprocess.TimeoutExpired:
        raise KeeperProbeError('keeper process query timed out') from None
    except (OSError, ValueError, KeyError, TypeError):
        raise KeeperProbeError('keeper process query unavailable') from None


def keeper_main_pid(unit):
    """Check user/system units, then the keeper process outside unit scopes."""
    errors = []
    for scope in ('user', 'system'):
        command = ['systemctl', *(['--user'] if scope == 'user' else []), 'show', unit,
                   '--property=LoadState', '--property=MainPID']
        try:
            result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=5)
        except FileNotFoundError:
            break  # No service manager: still check for a standalone keeper.
        except subprocess.TimeoutExpired:
            errors.append(f'keeper {scope} unit query timed out')
            continue
        except OSError:
            errors.append(f'keeper {scope} unit query unavailable')
            continue
        fields = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        if fields.get('LoadState') == 'not-found':
            continue
        if result.returncode:
            cause = 'bus unreachable' if 'bus' in result.stderr.lower() else 'query failed'
            errors.append(f'keeper {scope} unit {cause}')
            continue
        try:
            pid = int(fields['MainPID'])
            if fields.get('LoadState') != 'loaded' or pid < 0:
                raise ValueError('invalid unit state')
        except (KeyError, ValueError):
            errors.append(f'keeper {scope} unit state unreadable')
            continue
        if pid:
            return pid
    pid = keeper_process_pid()
    if pid:
        return pid
    if errors:
        raise KeeperProbeError('; '.join(errors))
    return pid


def main_ff_status_path(repo):
    return Path(repo_git(repo, 'rev-parse', '--absolute-git-dir').stdout.strip()) / 'plangraph-main-ff.status'
