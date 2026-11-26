"""Card-only per-host agent CLI: python -m plangraph.spawner.cli."""
from __future__ import annotations
import argparse
import asyncio
import json
from pathlib import Path
import sys
import os
import subprocess
import time
import uuid
_start_supervisor = subprocess.Popen
from .runner import spawn_service as SpawnService, redact_output
from plangraph.planner.routing import BenchmarkError, SpawnRefused
from plangraph.routing_support import background_process_options
from .base_fresh import admitted_spawn, run_exit_code, run_settled, spawn_runs, spawn_exit_code
from .seat_spawn import live_runner

def _wait_runs(service, result, wait):
    """Poll unfinished leaves within the wait limit, preserving terminal seat acknowledgements."""

    def pending(run):
        # Native records and explicit pending timestamps use durable settlement.
        # Legacy seat acknowledgements can be terminal without an ended field.
        if run.get('run_kind') == 'native-codex' or 'ended' in run:
            return not run_settled(run)
        return run.get('status') == 'running'

    deadline = time.monotonic() + max(0, wait)
    runs = spawn_runs(result)
    while any(pending(run) for run in runs) and time.monotonic() < deadline:
        interval = 2 if any(run.get('overflow_remote') for run in runs) else .02
        time.sleep(min(interval, max(0, deadline - time.monotonic())))
        for run in runs:
            if pending(run):
                run.update(_read_result(service, run['run_id']))
    if wait > 0:
        for run in runs:
            if (run.get('run_kind') == 'native-codex' and run.get('status') != 'running'
                    and not run_settled(run)):
                raise TimeoutError(f"{run['run_id']}: wait timed out before the run settled")

def _drain_chunks(chunks, write, observe=lambda size: None):
    """Bound unterminated stderr; retain 4 KiB for cross-chunk redaction."""
    from .env_redact import redact_env_values
    from .redact_tokens import open_wrapped_line_start
    from plangraph.routing_support.host_keys import redaction_env
    import codecs
    decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
    # The parent env never holds a file key; compute the redaction env (parent
    # plus resolvable host keys) once, not per chunk (D-2759).
    redact_base = redaction_env(os.environ)
    def clean(data):
        if isinstance(data, bytes):
            data = data.decode('utf-8')
        return redact_output(redact_env_values(data, redact_base)).encode()
    pending, held = b'', b''
    for chunk in chunks:
        pending += decoder.decode(chunk).encode('utf-8')
        observe(len(pending))
        while b'\n' in pending:
            line, pending = pending.split(b'\n', 1)
            held += line + b'\n'
            # Keep lines back while a wrapped credential may still continue.
            keep = open_wrapped_line_start(held.decode('utf-8'))
            if keep is not None and len(held) < 65536:
                keep = len(held.decode('utf-8')[:keep].encode('utf-8'))
                emit, held = held[:keep], held[keep:]
            else:
                emit, held = held, b''
            if emit:
                write(clean(emit))
        if len(pending) >= 65536:
            if held:
                write(clean(held))
                held = b''
            # Redact before choosing the boundary, so a known secret spanning
            # that boundary cannot leave either half in the output.
            safe = clean(pending)
            boundary = len(safe) - 4096
            while boundary > 0 and safe[boundary] & 0xc0 == 0x80:
                boundary -= 1
            write(safe[:boundary] + b' [truncated chunk]\n')
            pending = safe[boundary:]
    pending = held + pending + decoder.decode(b'', final=True).encode('utf-8')
    if pending:
        write(clean(pending.decode('utf-8')))

_SCOPE_PROBE = []


def scope_argv(argv):
    """``argv`` run inside its own transient systemd user scope when one is available.

    A supervisor started from a session is detached (own session) but still inside the session's
    cgroup, so stopping that session (a handoff) killed it and left its run unfinished. A scope of
    its own outlives the caller. The command is exec'd in place (same pid); without systemd,
    off Linux or with ``PLANGRAPH_SUPERVISOR_SCOPE=0`` the argv is returned unchanged.
    """
    # A replaced starter (a test double) is not a real launch: leave its argv alone.
    if (not sys.platform.startswith('linux') or os.environ.get('PLANGRAPH_SUPERVISOR_SCOPE') == '0'
            or _start_supervisor is not subprocess.Popen):
        return list(argv)
    if not _SCOPE_PROBE:
        try:
            probe = subprocess.run(['systemd-run', '--user', '--scope', '--quiet', '--collect', 'true'],
                                   stdin=subprocess.DEVNULL, capture_output=True, timeout=10, check=False)
            _SCOPE_PROBE.append(probe.returncode == 0)
        except (OSError, subprocess.TimeoutExpired):
            _SCOPE_PROBE.append(False)
    return ['systemd-run', '--user', '--scope', '--quiet', '--collect', *argv] if _SCOPE_PROBE[0] else list(argv)

def _rotate_log(path):
    """Move the full log to `path.1` (best effort) and empty `path`, which stays present throughout."""
    import shutil
    mode = 0o600
    tmp = path + '.1.tmp'
    try:
        with open(path, 'rb') as source, os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode), 'wb') as target:
            shutil.copyfileobj(source, target)
        os.replace(tmp, path + '.1')
    except OSError:
        pass
    os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode))

def _private_rotating_log(path, max_bytes=1024 * 1024):
    """Private (0600) size-rotating stderr handler; rotation goes through `_rotate_log`."""
    import logging
    from logging.handlers import RotatingFileHandler
    class PrivateLog(RotatingFileHandler):
        def _open(self): return os.fdopen(os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), 'a', encoding='utf-8', newline='')
        def shouldRollover(self, record): return self.stream.tell() + len(self.format(record).encode('utf-8')) + 1 > self.maxBytes
        def doRollover(self):
            self.stream.close()
            try:
                _rotate_log(self.baseFilename)
            finally:
                self.stream = self._open()
    logging.raiseExceptions = False
    handler = PrivateLog(path, maxBytes=max_bytes, backupCount=1)
    handler.terminator = ''
    return handler

def _rotation_source(handler=True):
    """Rotation helpers as source for the supervisor bootstrap (the run tree's own cli may predate them).
    The `-c` argument stays small, so a bootstrap that needs only the rotation passes handler=False."""
    import inspect
    return inspect.getsource(_rotate_log) + (inspect.getsource(_private_rotating_log) if handler else '')

def _prune_supervisor_logs(state):
    logs = []
    for path in state.glob('supervisor-*.stderr.log'):
        try:
            logs.append((path.stat().st_mtime, path))
        except FileNotFoundError:
            pass
    for modified, old in sorted(logs, reverse=True)[20:]:
        if time.time() - modified <= 86400 or os.name == 'nt':   # os.kill(pid, 0) terminates on Windows
            continue
        try:
            pid = int(Path(str(old) + '.pid').read_text())
            os.kill(pid, 0)
            continue
        except PermissionError:
            continue  # A live process owned by another user.
        except (OSError, ValueError):
            pass
        for path in (old, Path(str(old) + '.1')):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

def _status_line(run):
    """One redacted line for the launcher log: the durable status, its exit code, ALERT when refused."""
    status = str(run.get('status') or 'unknown')
    line = f"SPAWN {run.get('run_id')} {status.upper()}"
    reason = run.get('reason') or run.get('error')
    if reason:
        line += f": {redact_output(' '.join(str(reason).split()))}"   # the run id stays readable
    line += f' (exit {run_exit_code(run)})'
    return f'ALERT {line}' if status == 'refused' else line

def _exception_record(service, exc, role):
    """The saved record of a refusal raised during admission, found by the run id it carries.

    SeatSpawnMixin.spawn attaches the id after save_refusal; matching by error text could pick another
    run. An unreadable record yields a nonzero failure and never a second refusal."""
    run_id = getattr(exc, 'run_id', None)
    if run_id is None:
        return service.save_refusal(exc, role)
    try:
        return service.result(run_id)
    except (OSError, ValueError):
        return dict(run_id=run_id, status='failed', exit_code=1, ended=time.time(), refusal=type(exc).__name__,
                    error='refusal_record_unreadable')

def _supervise():
    payload = json.loads(sys.stdin.readline())
    service = SpawnService(state=payload.pop('state'))
    async def run():
        try:
            record = await admitted_spawn(service, payload)
        except Exception as exc:
            record = _exception_record(service, exc, payload.get('role', 'implement'))
            print(json.dumps(record), flush=True)
            print(_status_line(record), file=sys.stderr, flush=True)
            return run_exit_code(record)
        print(json.dumps(record), flush=True)
        sys.stdout.close()
        for run in spawn_runs(record):
            if run['run_id'] in service.tasks:
                await service.tasks[run['run_id']]
            elif run.get('status') in ('running', 'queued', 'dry_run'):
                continue  # no local task and nothing final to report
            # The admission snapshot can say 'running' or carry a stale code: the saved leaf decides,
            # whether a task ran or a refusal came back before any task existed.
            try:
                run.update(service.result(run['run_id']))
            except (OSError, ValueError):
                pass
            if run_settled(run):
                print(_status_line(run), file=sys.stderr, flush=True)
        return spawn_exit_code(record)
    return asyncio.run(run())

class _DeprecatedWriterFamily(argparse.Action):
    """``--writer-family`` still appends to the excluded families, with a warning: it reads like a reviewer choice."""

    def __call__(self, parser, namespace, values, option_string=None):
        print('warning: --writer-family is deprecated: it names the family EXCLUDED from review, '
              'not the reviewer; use --exclude-family', file=sys.stderr)
        setattr(namespace, self.dest, [*(getattr(namespace, self.dest, None) or []), values])


def _card(value):
    return json.loads(Path(value[1:]).read_text(encoding='utf-8') if value.startswith('@') else value)

def _read_result(service, run_id):
    """Local record read; asyncio.run only for a running remote native Codex run, so callers inside an event loop still work."""
    from .service import remote_native_running
    record = service.result(run_id)
    from .exit_outcome import with_stale
    if not remote_native_running(record):
        return with_stale(record)
    if record.get('role') == 'review' and record.get('run_kind') != 'native-codex':
        from .remote_ps import native_result
        updated = native_result(record)
        service._save(updated)
        return with_stale(updated)
    return asyncio.run(service.refresh_result(run_id))


def main(argv=None):
    if (argv if argv is not None else sys.argv[1:]) == ['--supervise']:
        return _supervise()
    parser = argparse.ArgumentParser(description='Spawn by card')
    parser.add_argument('--state')
    commands = parser.add_subparsers(dest='verb', required=True)
    for verb in ('route', 'spawn'):
        p = commands.add_parser(verb)
        p.add_argument('--card', type=_card)
        p.add_argument('--require-bench', action='store_true')
        p.add_argument('--bench-db', action='append')
        p.add_argument('--role', choices=('implement', 'review', 'orchestrate', 'research'), default='implement')
        # The family EXCLUDED from review (the writer's), not a reviewer choice.
        p.add_argument('--exclude-family', dest='writer_family', action='append', default=[])
        p.add_argument('--writer-family', dest='writer_family', action=_DeprecatedWriterFamily, default=[],
                       help='deprecated alias of --exclude-family')
        p.add_argument('--review-of')
        p.add_argument('--json', action='store_true')
        if verb == 'route':
            p.add_argument('--writer-snapshot', action='store_true', help=argparse.SUPPRESS)
            p.add_argument('--leg-scores', action='store_true',
                           help='add leg_scores: catalog intelligence (aa_intelligence) per routed leg id')
        if verb == 'spawn':
            p.add_argument('prompt', nargs='?')
            p.add_argument('--prompt-file')
            p.add_argument('--cwd', help='absolute path to an existing git checkout')
            p.add_argument('--review-scope', choices=['per-fixer', 'batch'])
            p.add_argument('--model')
            p.add_argument('--leg')
            p.add_argument('--override-reason')
            p.add_argument('--dry-run', action='store_true')
            p.add_argument('--run-worktree', action='store_true', help='isolate each attempt from origin/main')
            p.add_argument('--wait', type=float, default=0)
            p.add_argument('--no-fallthrough', action='store_true')
            p.add_argument('--seats', action='store_true', help='role review: spawn every seat route picks (per writer part)')
    commands.add_parser('ps').add_argument('--all-hosts', action='store_true',
                                           help='no-op alias: plain ps already includes configured SSH hosts')
    p = commands.add_parser('runs', help='read host lifecycle transitions')
    p.add_argument('--changed-since', required=True, type=json.loads, metavar='CURSOR_JSON')
    p = commands.add_parser('harness-report', help='runs, failures and no-verdict reviews per harness')
    p.add_argument('--hours', type=float, default=24)
    commands.add_parser('catalog')
    p = commands.add_parser('quota-result')
    p.add_argument('--model', required=True)
    p.add_argument('--harness', choices=('agy', 'claude', 'codex'), default='agy')
    p.add_argument('--error', required=True)
    p.add_argument('--reset-at', type=float)
    p.add_argument('--account')
    p.add_argument('--combo')
    commands.add_parser('launch-spec')
    p = commands.add_parser('probe')
    p.add_argument('--leg', action='append', default=[])
    p.add_argument('--all-writers', action='store_true')
    p.add_argument('--all-reviewers', action='store_true')
    p.add_argument('--timeout', type=float)
    p.add_argument('--json', action='store_true')
    p = commands.add_parser('wait')
    p.add_argument('run_id')
    p.add_argument('--timeout', type=float)
    for verb in ('result', 'status', 'cancel'):
        commands.add_parser(verb).add_argument('run_id')
    p = commands.add_parser('steer', help='queue a native writer message read from standard input')
    p.add_argument('run_id')
    p.add_argument('--host', help='configured target host when the run has no local receipt')
    p = commands.add_parser('resume', help='resume a native writer on its recorded host')
    p.add_argument('run_id')
    p.add_argument('--prompt-file', required=True, type=Path)
    args = parser.parse_args(argv)
    if args.verb == 'quota-result':
        from .env_redact import harness_limit_line
        from .exit_outcome import quota_models
        from .leg_state import record_harness_limit
        from .leg_probe import key
        from types import SimpleNamespace
        line = harness_limit_line(1, '', args.error)
        if line:
            if args.reset_at is not None and args.reset_at > time.time():
                line += f' Resets in {args.reset_at - time.time()}s.'
            for model in quota_models(args.model, args.harness):
                record_harness_limit(key(SimpleNamespace(model=model, harness=args.harness)),
                                     line, 'pg-spawnd:cli-result', harness=args.harness,
                                     account=args.account, combo=args.combo)
        print('{}')
        return 0
    if args.verb == 'catalog':
        from plangraph.routing_support.resolver import get_catalog_models
        from plangraph.planner.legs import REVIEW_UNFIT_SEATS
        print(json.dumps([dict(dict(id=row['id'], model=row.get('model', row['id']), roles=row.get('roles', [])),
                               **({'review_unfit': True} if row.get('model', row['id']) in REVIEW_UNFIT_SEATS else {}))
                          for row in get_catalog_models()]))
        return 0
    if args.verb == 'launch-spec':
        from . import launch_spec
        return launch_spec.main(sys.stdin.buffer, sys.stdout, sys.stderr)
    service = SpawnService(state=args.state)
    try:
        if args.verb == 'probe':
            from .leg_probe import ParameterRefused, probe_many, reset_iso
            try:
                if args.all_reviewers:
                    result = asyncio.run(probe_many(service.runner, args.leg, args.all_writers, args.timeout, all_reviewers=True))
                else:
                    result = asyncio.run(probe_many(service.runner, args.leg, args.all_writers, args.timeout))
            except ParameterRefused as exc:
                print(json.dumps(dict(status='refused', refusal='ParameterRefused', error=str(exc), exit_code=2)))
                return 2
            if args.json:
                print(json.dumps(result))
            else:
                for row in result['probes']:
                    if row.get('skipped') == 'limited':
                        # reset_iso guards legacy out-of-range reset_at values.
                        print(f"{row['id']} skipped=limited until {reset_iso(row.get('reset_at'))}")
                    else:
                        print(f"{row['id']} ok={row['ok']} {row['reason']} {row['seconds']:.2f}s")
            rows = result['probes']
            if rows and all(row.get('state') == 'host-not-ready' for row in rows):
                return 3
            return 0 if result['ok'] else 1
        from .bench_entry import benchmark_options
        try:
            options = benchmark_options(args.require_bench, args.bench_db) if args.verb in ('route', 'spawn') else {}
        except Exception as exc:
            if args.verb in ('route', 'spawn'):
                service.save_refusal(exc, args.role)
            raise
        if args.verb == 'route':
            if args.role == 'review' and args.json and (args.card or {}).get('repository_review_required') is True:
                options['repository_preview'] = True
            routed = service.route(args.card, args.role, args.writer_family, args.review_of, **options)
            result = routed.to_dict()
            if args.role == 'review':
                from plangraph.planner.route_policy import load_route_policy
                # The native spawner queues seats beyond a leg's bound from this (seat_admission.rs).
                admission = load_route_policy().review_seat_admission
                if admission:
                    result['seat_admission'] = dict(admission)
            if args.leg_scores:
                from plangraph.planner.review_order import catalog_score
                from plangraph.routing_support.resolver import get_catalog_models
                rows = get_catalog_models()
                result['leg_scores'] = {leg.id: catalog_score(leg, rows) for leg in routed.legs}
            if args.writer_snapshot:
                from plangraph.planner.legs import DEFAULT_LEGS
                from plangraph.planner.review_order import catalog_score
                from plangraph.routing_support.resolver import get_catalog_models
                writers = result if args.role == 'implement' else service._route(args.card, 'implement').to_dict()
                served = {leg['id'] for leg in writers['legs']}
                skipped = {row['leg']: row['reason'] for row in writers['skipped']}
                rows = get_catalog_models()
                result['work_mode_writer_legs'] = [dict(
                    id=leg.id, aa_intelligence=catalog_score(leg, rows), served=leg.id in served,
                    unavailable_reason=skipped.get(leg.id) if leg.id not in served else None)
                    for leg in DEFAULT_LEGS if 'implement' in leg.roles]
        elif args.verb == 'spawn':
            from plangraph.planner.route_policy import refuse_card_sandbox
            try:
                refuse_card_sandbox(args.card)   # before model resolution or any supervisor start
            except SpawnRefused as exc:
                service.save_refusal(exc, args.role)
                raise
            if args.model is not None:
                from plangraph.planner.routing import validate_model
                try:
                    validate_model(args.model)
                except Exception as exc:
                    service.save_refusal(exc, args.role)
                    raise
            if not args.card:
                exc = SpawnRefused('spawn by card; card required')
                service.save_refusal(exc, args.role)
                raise exc
            from .prompt_input import resolve_prompt
            try:
                prompt = resolve_prompt(args.prompt, args.prompt_file)
                from .prompt_guard import admit_prompt
                if args.role == 'implement': admit_prompt(prompt, SpawnRefused)
            except SpawnRefused as exc:
                service.save_refusal(exc, args.role)
                raise
            payload = dict(card=args.card, prompt=prompt, role=args.role, writer_families=args.writer_family,
                review_of=args.review_of, model=args.model, review_scope=args.review_scope,
                run_worktree=args.run_worktree, cwd=args.cwd, leg=args.leg, override_reason=args.override_reason,
                dry_run=args.dry_run, no_fallthrough=args.no_fallthrough, seats=args.seats, **options)
            from plangraph.settings import admit_review_scope
            try:
                admit_review_scope(args.card, args.role, args.review_scope)
            except SpawnRefused as exc:
                service.save_refusal(exc, args.role)
                raise
            from .inline_diff import refuse_empty_diff
            try:
                refuse_empty_diff(args.card, args.role, dry_run=args.dry_run)
            except SpawnRefused as exc:
                service.save_refusal(exc, args.role)
                raise
            # Explicit model/leg overrides intentionally bypass routing, not admission.
            if not args.model and not args.leg and (args.card or {}).get('run_kind') != 'native-codex':
                # service.route saves the refusal record itself (one record per route error).
                service.route(args.card, args.role, args.writer_family, args.review_of, **options)
            if args.dry_run:
                result = asyncio.run(service.spawn(**payload, wait_seconds=None))
            elif (live_runner(getattr(service, 'runner', None)) and _start_supervisor is subprocess.Popen
                  and (args.card or {}).get('run_kind') != 'native-codex'):
                service.fresh_tree = True
                async def fresh():
                    result = await admitted_spawn(service, payload)
                    await asyncio.to_thread(_wait_runs, service, result, args.wait)
                    for task in set(service.tasks.values()): task.cancel()
                    await asyncio.gather(*service.tasks.values(), return_exceptions=True)
                    return result
                result = asyncio.run(fresh())
            else:
                if (live_runner(getattr(service, 'runner', None)) and (args.card or {}).get('run_kind') != 'native-codex'
                        and _start_supervisor is not subprocess.Popen):
                    print('warning: fresh-tree handoff skipped (supervisor starter replaced); records carry process-load tree identity',
                          file=sys.stderr)
                import plangraph
                env = os.environ.copy()
                root = str(Path(plangraph.__file__).resolve().parent.parent)
                try:
                    installed = subprocess.run([sys.executable, '-I', '-c', 'import plangraph'],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10).returncode == 0
                except (OSError, subprocess.TimeoutExpired):
                    installed = False
                checkout = (any((Path(root).parent / name).is_file() for name in ('pyproject.toml', 'setup.cfg'))
                            or (Path(root).name == 'src' and (Path(root).parent / '.git').exists()))
                if checkout:
                    env['PYTHONPATH'] = root + (os.pathsep + env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
                elif not installed:
                    env['PYTHONPATH'] = (env['PYTHONPATH'] + os.pathsep if env.get('PYTHONPATH') else '') + root
                # Drain fd 2 in the child so Python AND native writes rotate, even
                # after the launching CLI exits. Keep the startup tail readable.
                service.state.mkdir(parents=True, exist_ok=True)
                log_path = service.state / ('supervisor-' + uuid.uuid4().hex + '.stderr.log')
                _prune_supervisor_logs(service.state)
                bootstrap = '''import os, pathlib, sys, threading
from plangraph.spawner import cli as supervisor
''' + _rotation_source(handler=False) + '''from plangraph.spawner.env_redact import redact_env_values
from plangraph.routing_support.host_keys import redaction_env
# The parent env never holds a file key; compute the redaction env once at
# bootstrap top, not per emitted line (D-2759).
redact_base = redaction_env(os.environ)
path = sys.argv[1]
if sys.platform.startswith("linux"):
    from plangraph.spawner.proc_kill import _stat
try:
    pathlib.Path(path + ".pid").write_text(str(os.getpid()))
except OSError:
    pass
read_fd, write_fd = os.pipe()
os.dup2(write_fd, 2)
os.close(write_fd)
def drain():
    def open_log():
        return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "ab", buffering=0)
    output = None
    try:
        try:
            output = open_log()
        except OSError:
            pass
        def emit(line):
            nonlocal output
            data = supervisor.redact_output(redact_env_values(line.decode("utf-8", errors="replace"), redact_base)).encode()
            # Bound even a native writer's multi-megabyte unterminated line,
            # but only after redaction so truncation cannot expose a secret suffix.
            data = data[-1024 * 1024:]
            if output is None:
                return
            try:
                if output.tell() + len(data) > 1024 * 1024:
                    output.close()
                    output = None
                    _rotate_log(path)
                    output = open_log()
                output.write(data)
            except OSError:
                if output is not None:
                    output.close()
                output = None
        def chunks():
            while True:
                chunk = os.read(read_fd, 8192)
                if not chunk:
                    return
                yield chunk
        supervisor._drain_chunks(chunks(), emit)
    finally:
        if output is not None:
            output.close()
        os.close(read_fd)
thread = threading.Thread(target=drain, daemon=True)
thread.start()
factory = supervisor.SpawnService
def service_with_log(*args, **kwargs):
    service = factory(*args, **kwargs)
    save = service._save
    def save_with_log(record):
        record["supervisor_stderr_path"] = path
        record["supervisor_pid"] = os.getpid()
        record["supervisor_pid_start"] = (_stat(os.getpid()) or (None,) * 5)[4] if sys.platform.startswith("linux") else None
        return save(record)
    service._save = save_with_log
    return service
supervisor.SpawnService = service_with_log
try:
    code = supervisor.main(["--supervise"])
except Exception:
    import traceback
    traceback.print_exc()
    code = 1
finally:
    sys.stderr.flush()
    os.close(2)
    thread.join(timeout=5)
raise SystemExit(code)
'''
                with os.fdopen(os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), 'ab') as errors:
                    child = _start_supervisor(scope_argv([sys.executable, '-c', bootstrap, str(log_path)]),
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors,
                        env=env, text=True, encoding="utf-8", errors="replace", **background_process_options(new_session=True, platform=os.name))
                    try:
                        child.stdin.write(json.dumps(dict(payload, state=str(service.state))) + '\n')
                        child.stdin.close()
                    except BrokenPipeError:
                        pass  # Read the startup diagnostic below, including early import failures.
                    line = child.stdout.readline()
                    child.stdout.close()
                    if not line:
                        child.wait()
                        from .env_redact import redact_env_values
                        from .opencode_overlay import overlay_env
                        from plangraph.routing_support.host_keys import redaction_env
                        text = ''
                        size = 0
                        for path in (Path(str(log_path) + '.1'), log_path):
                            try:
                                with path.open('rb') as current:
                                    current.seek(0, os.SEEK_END)
                                    size = current.tell()
                                    current.seek(max(0, size - 262144))
                                    text += current.read(262144).decode('utf-8', errors='replace')
                            except FileNotFoundError:
                                pass
                        # Discard the possibly partial first line before displaying a suffix.
                        if size > 262144:
                            text = text.partition('\n')[2]
                        tail = '\n'.join(redact_output(redact_env_values(text, redaction_env(overlay_env(os.environ)))).splitlines()[-20:])
                        tail = tail.encode()[-4096:].decode('utf-8', errors='ignore')
                        raise ValueError('spawn supervisor failed; check card and override reason' +
                                         ('\n' + tail if tail else ''))
                result = json.loads(line)
                _wait_runs(service, result, args.wait)
        elif args.verb == 'ps':
            from .remote_ps import list_runs
            result = {'runs': list_runs(service.ps()['runs'])}
        elif args.verb == 'runs':
            from .remote_ps import changed_runs
            result = changed_runs(service.state, args.changed_since)
        elif args.verb == 'harness-report':
            from .exit_outcome import harness_report
            result = harness_report(service.ps()['runs'], hours=args.hours)
        elif args.verb == 'cancel':
            result = asyncio.run(service.cancel(args.run_id))
        elif args.verb == 'steer':
            from .remote_ps import steer_run
            result = steer_run(service, args.run_id, sys.stdin.read(16385), args.host)
        elif args.verb == 'resume':
            from .remote_ps import MAX_OUTPUT, resume_run
            with args.prompt_file.open('rb') as source:
                prompt = source.read(MAX_OUTPUT + 1)
            if len(prompt) > MAX_OUTPUT:
                raise ValueError('resume prompt must contain between 1 byte and 1 MiB of text')
            result = resume_run(service, args.run_id, prompt.decode('utf-8'))
        elif args.verb == 'wait':
            result = asyncio.run(service.wait(args.run_id, timeout=args.timeout))
        else:
            result = _read_result(service, args.run_id)
        if isinstance(result, dict):
            seen = set()
            for skipped in list(result.get('skipped', [])) + list(result.get('attempts', [])):
                reason = skipped.get('skip_reason') or skipped.get('reason')
                if not reason:
                    continue
                leg = skipped.get('leg')
                name = leg.get('id', leg.get('model')) if isinstance(leg, dict) else leg
                line = f'skip {name}: {reason}'
                if line not in seen:
                    print(line, file=sys.stderr)
                    seen.add(line)
        print(json.dumps(result, indent=2))
        if isinstance(result, dict):
            if args.verb == 'spawn' and (args.card or {}).get('run_kind') == 'native-codex':
                return spawn_exit_code(result)
            if 'writers' in result:
                for run in spawn_runs(result):
                    if run.get('reason'): print(run['run_id'] + ': ' + run['reason'], file=sys.stderr)
                return spawn_exit_code(result)
            if 'seats' in result:
                # rc 0 only while some seat can still deliver a verdict (planned, running or completed).
                return 0 if any(r.get('status') in ('dry_run', 'running', 'completed') for r in result['seats']) else 1
            if 'status' not in result:
                if args.verb == 'route' and options.get('repository_preview') and not result.get('legs'):
                    return 0
                return 2 if result.get('error') else 0
            if args.verb in ('spawn', 'wait'):
                return run_exit_code(result)   # one terminal rule for `spawn --wait` and `wait RUN_ID`
            code = result.get('exit_code')
            return code if code is not None else (0 if result.get('status') == 'running' else 1)
        return 0
    except (ValueError, KeyError, OSError, RuntimeError, subprocess.SubprocessError, SpawnRefused) as exc:
        if (args.verb == 'route' and args.json and args.role == 'research'
                and isinstance(exc, SpawnRefused) and getattr(exc, 'skipped', None)):
            # The spawnd adapter retains only the final stderr line.
            print(json.dumps({'error': str(exc)}))
        print(((f'{args.run_id}: ' if hasattr(args, 'run_id') else '') + type(exc).__name__) if isinstance(exc, PermissionError) else str(exc), file=sys.stderr)
        return 3 if isinstance(exc, BenchmarkError) else 2 if isinstance(exc, SpawnRefused) else 1

if __name__ == '__main__':
    raise SystemExit(main())
