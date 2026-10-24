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

_start_supervisor = subprocess.Popen

from .runner import spawn_service as SpawnService, redact
from plangraph.planner.routing import BenchmarkError, SpawnRefused


def _supervise():
    payload = json.loads(sys.stdin.readline())
    service = SpawnService(state=payload.pop('state'))
    async def run():
        try:
            record = await service.spawn(**payload, wait_seconds=0)
        except Exception as exc:
            record = next((r for r in service.ps()['runs'] if r.get('error') == str(exc)), None)
            if record is None:
                record = service.save_refusal(exc, payload.get('role', 'implement'))
            print(json.dumps(record), flush=True)
            return record['exit_code']
        print(json.dumps(record), flush=True)
        sys.stdout.close()
        if record['run_id'] in service.tasks:
            await service.tasks[record['run_id']]
        return 0
    return asyncio.run(run())


def _card(value):
    return json.loads(Path(value[1:]).read_text(encoding='utf-8') if value.startswith('@') else value)


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
        p.add_argument('--role', choices=('implement', 'review'), default='implement')
        p.add_argument('--writer-family', action='append', default=[])
        p.add_argument('--review-of')
        p.add_argument('--json', action='store_true')
        if verb == 'spawn':
            p.add_argument('prompt')
            p.add_argument('--model')
            p.add_argument('--leg')
            p.add_argument('--override-reason')
            p.add_argument('--dry-run', action='store_true')
            p.add_argument('--wait', type=float, default=0)
            p.add_argument('--no-fallthrough', action='store_true')
    commands.add_parser('ps')
    p = commands.add_parser('wait')
    p.add_argument('run_id')
    p.add_argument('--timeout', type=float)
    for verb in ('result', 'status', 'cancel'):
        commands.add_parser(verb).add_argument('run_id')
    args = parser.parse_args(argv)
    service = SpawnService(state=args.state)
    try:
        from .bench_entry import benchmark_options
        try:
            options = benchmark_options(args.require_bench, args.bench_db) if args.verb in ('route', 'spawn') else {}
        except Exception as exc:
            if args.verb in ('route', 'spawn'):
                service.save_refusal(exc, args.role)
            raise
        if args.verb == 'route':
            result = service.route(args.card, args.role, args.writer_family, args.review_of, **options).to_dict()
        elif args.verb == 'spawn':
            if args.model:
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
            payload = dict(card=args.card, prompt=args.prompt, role=args.role,
                writer_families=args.writer_family, review_of=args.review_of, model=args.model,
                leg=args.leg, override_reason=args.override_reason, dry_run=args.dry_run,
                no_fallthrough=args.no_fallthrough, **options)
            # Validate before detaching so refusals are reported synchronously.
            if not args.model and not args.leg:
                if not args.card:
                    raise ValueError('spawn by card; card required')
                try:
                    service.route(args.card, args.role, args.writer_family, args.review_of, **options)
                except Exception as exc:
                    service.save_refusal(exc, args.role)
                    raise
            if args.dry_run:
                result = asyncio.run(service.spawn(**payload, wait_seconds=None))
            else:
                import plangraph
                env = os.environ.copy()
                root = str(Path(plangraph.__file__).resolve().parent.parent)
                installed = subprocess.run([sys.executable, '-I', '-c', 'import plangraph'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10).returncode == 0
                if not installed:
                    env['PYTHONPATH'] = (env['PYTHONPATH'] + os.pathsep if env.get('PYTHONPATH') else '') + root
                # Drain fd 2 in the child so Python AND native writes rotate, even
                # after the launching CLI exits. Keep the startup tail readable.
                service.state.mkdir(parents=True, exist_ok=True)
                log_path = service.state / 'supervisor.stderr.log'
                bootstrap = '''import os, sys, threading
from plangraph.spawner import cli as supervisor
path = sys.argv[1]
read_fd, write_fd = os.pipe()
os.dup2(write_fd, 2)
os.close(write_fd)
def drain():
    def open_log():
        return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "ab", buffering=0)
    output = open_log()
    try:
        while True:
            chunk = os.read(read_fd, 8192)
            if not chunk:
                break
            if output.tell() + len(chunk) > 1024 * 1024:
                output.close()
                os.replace(path, path + ".1")
                output = open_log()
            output.write(chunk)
    finally:
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
        return save(record)
    service._save = save_with_log
    return service
supervisor.SpawnService = service_with_log
try:
    code = supervisor.main(["--supervise"])
except BaseException:
    import traceback
    traceback.print_exc()
    code = 1
finally:
    sys.stderr.flush()
    os.close(2)
    thread.join(timeout=5)
raise SystemExit(code)
'''
                with open(log_path, 'a+b') as errors:
                    os.chmod(log_path, 0o600)
                    child = _start_supervisor([sys.executable, '-c', bootstrap, str(log_path)],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors,
                        env=env, text=True, **({'start_new_session': True} if os.name != 'nt' else
                                      {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP}))
                    try:
                        child.stdin.write(json.dumps(dict(payload, state=str(service.state))) + '\n')
                        child.stdin.close()
                    except BrokenPipeError:
                        pass  # Read the startup diagnostic below, including early import failures.
                    line = child.stdout.readline()
                    child.stdout.close()
                    if not line:
                        child.wait()
                        errors.seek(0, os.SEEK_END)
                        size = errors.tell()
                        errors.seek(max(0, size - 262144))
                        from .env_redact import redact_env_values
                        from .opencode_overlay import overlay_env
                        text = errors.read(262144).decode('utf-8', errors='replace')
                        # Discard the possibly partial first line before displaying a suffix.
                        if size > 262144:
                            text = text.partition('\n')[2]
                        tail = '\n'.join(redact(redact_env_values(text, overlay_env(os.environ))).splitlines()[-20:])
                        tail = tail.encode()[-4096:].decode('utf-8', errors='ignore')
                        raise ValueError('spawn supervisor failed; check card and override reason' +
                                         ('\n' + tail if tail else ''))
                result = json.loads(line)
                deadline = time.monotonic() + max(0, args.wait)
                while result['status'] == 'running' and time.monotonic() < deadline:
                    time.sleep(min(.02, max(0, deadline - time.monotonic())))
                    result = service.result(result['run_id'])
        elif args.verb == 'ps':
            result = service.ps()
        elif args.verb == 'cancel':
            result = asyncio.run(service.cancel(args.run_id))
        elif args.verb == 'wait':
            result = asyncio.run(service.wait(args.run_id, timeout=args.timeout))
        else:
            result = service.result(args.run_id)
        print(json.dumps(result, indent=2))
        if isinstance(result, dict):
            if 'status' not in result:
                return 2 if result.get('error') else 0
            code = result.get('exit_code')
            return code if code is not None else (0 if result.get('status') == 'running' else 1)
        return 0
    except (ValueError, KeyError, OSError, SpawnRefused) as exc:
        print(str(exc), file=sys.stderr)
        return 3 if isinstance(exc, BenchmarkError) else 2 if isinstance(exc, SpawnRefused) else 1


if __name__ == '__main__':
    raise SystemExit(main())
