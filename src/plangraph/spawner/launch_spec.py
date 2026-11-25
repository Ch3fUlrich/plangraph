"""Prepare one harness launch without starting it.

Not done here: leg_probe.before_launch, fall-through to other legs, served-model
witness and nonce bookkeeping (the nonce header is built, not registered), git
config guards, writer no-change baseline, watchdog thresholds, or live.log.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
import os
from pathlib import Path
import re
import shutil
import tempfile
import time

from plangraph.planner.legs import DEFAULT_LEGS, LEG_ROLES, opencode_counterpart
from plangraph.planner.routing import SpawnRefused
from . import context_pack, runner
from .git_guard import writer_launch
from .host_ready import harness_path
from .inline_diff import InlineDiffError, needs_inline_diff
from .resolver import host_skip_reason_async, admitted_by_render
from .service import SpawnService


class LaunchRefused(Exception):
    def __init__(self, kind, reason):
        self.kind, self.reason = kind, reason
        super().__init__(reason)


class InvalidRequest(Exception):
    pass


class _LaunchOwned(dict):
    """Transfer dry-run resources to the external launcher, not runner cleanup."""

    def pop(self, key, default=None):
        return default


def _validate(req):
    if not isinstance(req, dict):
        raise InvalidRequest('json')
    checks = dict(
        run_id=lambda v: isinstance(v, str) and re.fullmatch(r'run-[0-9a-f]{32}', v),
        job_id=lambda v: isinstance(v, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', v),
        role=lambda v: isinstance(v, str) and v in LEG_ROLES,
        dry_run=lambda v: type(v) is bool,
        leg=lambda v: isinstance(v, dict) and isinstance(v.get('id'), str) and bool(v['id'])
            and ('explicit' not in v or type(v['explicit']) is bool),
        prompt=lambda v: isinstance(v, str), card=lambda v: isinstance(v, dict),
        cwd=lambda v: isinstance(v, str) and os.path.isabs(v) and os.path.isdir(v),
        state=lambda v: isinstance(v, str) and os.path.isabs(v))
    for key, check in checks.items():
        if key not in req or not check(req[key]):
            raise InvalidRequest(key)
    if 'writer_families' in req and (not isinstance(req['writer_families'], list)
            or not all(isinstance(w, str) for w in req['writer_families'])):
        raise InvalidRequest('writer_families')
    if req.get('review_of') is not None and not isinstance(req['review_of'], str):
        raise InvalidRequest('review_of')
    for key in req:
        if key not in checks and key not in ('writer_families', 'review_of'):
            raise InvalidRequest(key)


def _sweep(service, current):
    try:
        dirs = list(current.parent.iterdir())
    except OSError:
        return
    for path in dirs:
        try:
            if path == current or path.is_symlink() or not path.is_dir():
                continue
            try:
                status = json.loads(service._path(path.name).read_text())['status']
                remove = status not in ('queued', 'running')
            except (OSError, ValueError, KeyError, TypeError):
                remove = time.time() - path.stat().st_mtime > 86400
            if remove:
                shutil.rmtree(path)
        except Exception:
            pass  # A stale directory must never fail a launch request.


def _refuse_sandbox(request):
    """Refuse a wider sandbox before any client lookup, run tree or leg resolution."""
    from plangraph.planner.route_policy import refuse_card_sandbox
    from plangraph.routing_errors import SpawnRefused
    try:
        refuse_card_sandbox(request['card'])
    except SpawnRefused as exc:
        raise LaunchRefused('sandbox_refused', str(exc).partition(': ')[2]) from None


def _refusal_line(exc):
    line = f'{exc.kind}: {runner.redact(str(exc.reason))}'
    return line.replace('\r', ' ').replace('\n', ' ')[:300] + '\n'


def build(request: dict) -> dict:
    _validate(request)
    _refuse_sandbox(request)
    from .prompt_guard import admit_prompt
    if request['role'] == 'implement':
        admit_prompt(request['prompt'], lambda reason: LaunchRefused('refused', reason))
    from plangraph.planner.legs import EXPLICIT_LEGS   # this module's DEFAULT_LEGS stays patchable
    leg = next((leg for leg in (*DEFAULT_LEGS, *EXPLICIT_LEGS) if leg.id == request['leg']['id']), None)
    if leg is None:
        raise LaunchRefused('unknown_leg', 'unknown leg id')
    if leg.family == 'gpt' and leg.harness == 'codex':
        planned_harness = request['leg'].get('harness')
        if planned_harness == 'opencode':
            counterpart = opencode_counterpart(leg)
            if counterpart is None:
                raise LaunchRefused('leg_harness', 'no OpenCode counterpart for ' + leg.id)
            leg = counterpart
        elif planned_harness is None and request['role'] != 'review':
            # Older non-review callers supply only the registered leg ID.
            from plangraph.planner.legs import resolve_harness
            choice = resolve_harness(leg)
            if choice.leg is None:
                raise LaunchRefused('client_missing', 'Codex client unavailable for ' + leg.id)
            leg = choice.leg
    codex = None
    if not request['dry_run'] and leg.harness == 'codex' and request['role'] == 'review':
        from plangraph.routing_support.clients import codex_review_client
        env = dict(os.environ, PATH=harness_path(os.environ))
        codex, missing_client = codex_review_client(leg.harness, request['role'], request['run_id'], env=env)
        if missing_client:
            raise LaunchRefused('client_missing', missing_client)
    from plangraph.planner.route_policy import codex_leg_effort
    from .target import admit_leg, quota_paused
    if quota_paused(request['state'], request['role'], leg):
        raise LaunchRefused('quota_pause', 'quota pause')
    from plangraph.routing_errors import SpawnRefused
    try:
        admit_leg(leg)
        if request['role'] in ('implement', 'review'):
            leg = codex_leg_effort(leg, request['card'])
    except SpawnRefused as exc:
        reason = str(exc)
        raise LaunchRefused(reason.split(':', 1)[0] if reason.startswith('effort_') else 'leg_inactive', reason) from None
    if request['role'] not in leg.roles:
        raise LaunchRefused('leg_role', f"role {request['role']} not in leg roles")
    from .target import effective_leg
    from plangraph.routing_support.opencode_overlay import ProviderView
    try:
        view = ProviderView() if leg.harness == 'opencode' else None
        leg = effective_leg(leg, request['card'], request['role'], view=view)
    except SpawnRefused as exc:
        raise LaunchRefused('request_bound', str(exc)) from None
    if request['role'] == 'research' and request['card'].get('web_required') and not leg.has_web_tools:
        raise LaunchRefused('web_access_unavailable', leg.id)
    service = SpawnService(state=request['state'])
    reader = getattr(service, 'leg_reader', None)
    if leg.harness in ('agy', 'codex'):
        from plangraph.routing_support.liveness import require_available
        try:
            require_available(leg, reader, request['card'])
        except SpawnRefused as exc:
            raise LaunchRefused('leg_unavailable', str(exc)) from None
    from plangraph.routing_support.quota_snapshots import pool_of, skip_reason
    if reader is not None and leg.harness != 'codex' and pool_of(leg.model):
        card = request['card']
        status = reader.leg_status(leg.model, account=card.get('account'), combo=card.get('combo'))
        if reason := skip_reason(status):
            raise LaunchRefused('quota_snapshot', reason)
    headers = None
    if request['role'] == 'review':
        from plangraph.planner.seats import seat_family
        from plangraph.review.family import FamilyError, writer_headers, writer_key
        writers = list(request.get('writer_families') or [])
        try:
            if request.get('review_of'):
                try:
                    source = service.result(request['review_of'])
                except ValueError as exc:  # unknown run id
                    raise FamilyError(str(exc)) from None
                writers.extend(source.get('given_writer_families') or [source.get('writer_family')])
            # The card's head Writer trailers join the caller's families: one set for the gateway headers and the check.
            from plangraph.planner.seats import with_head_writers
            writers = with_head_writers(request['card'], 'review', writers)
            headers = writer_headers(writers)
            if seat_family(leg.family) in {writer_key(w) for w in writers}:
                raise FamilyError('review seat is a writer family')
        except (FamilyError, SpawnRefused) as exc:
            raise LaunchRefused('writer_family', str(exc)) from None
    service.runner.default_cwd = request['cwd']
    service.runner._child_envs = _LaunchOwned()
    service.runner._ocdata_dirs = _LaunchOwned()
    current = service.state / 'launch' / request['run_id']
    current.mkdir(mode=0o700, parents=True, exist_ok=True)
    current.chmod(0o700)
    previous = tempfile.tempdir
    tempfile.tempdir = str(current)
    try:
        _sweep(service, current)
        rec = dict(run_id=request['run_id'], role=request['role'], cwd=request['cwd'], warnings=[], events=[])
        try:
            pack = context_pack.annotate(rec, request['prompt'], request['card'], request['cwd'])
        except SpawnRefused as exc:
            raise LaunchRefused('context', str(exc)) from None
        except context_pack.ContextError as exc:
            raise LaunchRefused('context', str(exc)) from None
        card = request['card']
        role = request['role']
        reference = card.get('diff_ref')
        registered_diff = role == 'review' and isinstance(reference, dict) and 'repo_id' in reference
        can_split = (role == 'review' and isinstance(reference, dict) and 'repo' in reference
                     and not registered_diff and 'paths' not in reference and needs_inline_diff(leg, card))
        split_inline_diff = False

        def prepare(defer_inline_diff):
            prompt = service._leg_prompt(request['prompt'], card, role, leg, pack,
                                         document_review=request['leg'].get('explicit') is True,
                                         prepared=True, defer_inline_diff=defer_inline_diff)
            prompt, writer_env = writer_launch(prompt, role, leg.model, request['run_id'])
            return asyncio.run(service.runner.spawn(prompt=prompt, model_or_combo=leg.model,
                role=role, dry_run=True, client=leg.harness, chosen=leg, extra_env=writer_env,
                prepare_launch=True, headers=headers))

        try:
            try:
                job = prepare(registered_diff)
            except context_pack.PromptBoundError:
                if not can_split:
                    raise
                # Splitting is useful only when the composed prompt without the diff fits.
                job = prepare(True)
                split_inline_diff = True
        except context_pack.PromptBoundError as exc:
            shutil.rmtree(current, ignore_errors=True)
            raise LaunchRefused('inline_diff_failed', str(exc)) from None
        except InlineDiffError as exc:
            raise LaunchRefused('inline_diff_failed', str(exc)) from None
        except (SpawnRefused, ValueError) as exc:
            raise LaunchRefused('refused', str(exc)) from None
        for event in job.events:
            if event.get('event') == 'render_refused':
                raise LaunchRefused('render_refused', event['reason'])
        cmd = job.command
        from plangraph.routing_support.clients import PromptTransportError, launch_stdin_payload
        try:
            payload = launch_stdin_payload(cmd, job.task_prompt, cmd[0])
        except PromptTransportError as exc:
            kind, _, detail = str(exc).partition(': ')
            raise LaunchRefused(kind, detail) from None
        env = service.runner._child_envs[job.job_id]
        argv, events = list(cmd), rec['events'] + job.events
        if not request['dry_run']:
            reason = asyncio.run(host_skip_reason_async(dict(model=leg.model, harness=cmd[0]), env=env))
            if reason:
                raise LaunchRefused('unavailable', reason)
            if admitted_by_render(leg.model):
                events.append(dict(event='admitted_by_render', model=leg.model))
            exe = codex or shutil.which(cmd[0], path=env.get('PATH')) or cmd[0]
            exe, reason = runner._resolve_launch(exe, cmd[1:], is_nt=runner._is_nt())
            if reason:
                raise LaunchRefused('launch', reason)
            argv[0] = exe
        if runner._is_nt():
            for name in ('SystemRoot', 'COMSPEC', 'PATHEXT', 'TEMP'):
                if name.lower() not in {key.lower() for key in env}:
                    raise WindowsEnvIncomplete(name)
        events.append(dict(event='launch_spec', python_job_id=job.job_id))
        return dict(argv=argv, cwd=os.path.abspath(request['cwd']), env=env,
                    prompt=job.task_prompt,  # REMOVE-AT-R10: Rust redacts the final prompt.
                    stdin=payload.decode('utf-8') if payload is not None else None,
                    warnings=[w for w in rec['warnings'] if isinstance(w, str)], events=events,
                    max_request_tokens=job.max_request_tokens, split_inline_diff=split_inline_diff)
    finally:
        tempfile.tempdir = previous


class WindowsEnvIncomplete(Exception):
    pass


def _run_group(argv, text, cwd, env, timeout=60):
    """Run the tree CLI in its own process group; a timeout kills the whole group."""
    import signal, subprocess
    child = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, cwd=cwd, env=env,
                             text=True, encoding='utf-8', errors='replace', start_new_session=os.name != 'nt')
    try:
        out, _ = child.communicate(text, timeout=timeout)
    except BaseException:
        try: os.killpg(child.pid, signal.SIGKILL)
        except (AttributeError, OSError): child.kill()
        child.communicate()
        raise
    return SimpleNamespace(returncode=child.returncode, stdout=out)


def fresh_main():
    """Import launch construction in the fetched SHA tree, not the daemon's package."""
    import subprocess, sys
    from .git_guard import run_tree
    try:
        raw = sys.stdin.read(16 * 1024 * 1024 + 1)
        if len(raw) > 16 * 1024 * 1024: raise InvalidRequest('json')
        try: request = json.loads(raw)
        except (ValueError, UnicodeError): raise InvalidRequest('json') from None
        _validate(request)
        _refuse_sandbox(request)
    except InvalidRequest as exc:
        sys.stderr.write(f'launch-spec failed: invalid request ({exc})\n')
        return 1
    except LaunchRefused as exc:
        sys.stderr.write(_refusal_line(exc))
        return 2
    try:
        with run_tree(request['state']) as tree:
            env = dict(os.environ, PYTHONPATH=os.pathsep.join(filter(None, [str(Path(tree['tree_path']) / 'src'), os.environ.get('PYTHONPATH')])),
                       PYTHONDONTWRITEBYTECODE='1')
            result = _run_group([sys.executable, '-m', 'plangraph.spawner.cli',
                '--state', request['state'], 'launch-spec'], json.dumps(request), tree['tree_path'], env)
            if result.returncode == 0:
                try: spec = json.loads(result.stdout)
                except ValueError:
                    sys.stderr.write('launch-spec failed: tree CLI wrote non-JSON stdout\n')
                    return 1
                spec['events'] = [*(spec.get('events') or []), dict(event='run_tree', **tree)]
                print(json.dumps(spec))
            return result.returncode
    except Exception as exc:
        sys.stderr.write(f'launch-spec failed: {type(exc).__name__}\n')
        return 1


def main(stdin, stdout, stderr) -> int:
    try:
        try:
            raw = stdin.read(16 * 1024 * 1024 + 1)
            if len(raw) > 16 * 1024 * 1024:
                raise ValueError
            request = json.loads(raw.decode('utf-8') if isinstance(raw, bytes) else raw)
        except (ValueError, UnicodeError):
            raise InvalidRequest('json') from None
        result = build(request)
        output = json.dumps(result)
        stdout.write(output + '\n')
        return 0
    except InvalidRequest as exc:
        stderr.write(f'launch-spec failed: invalid request ({exc})\n')
        return 1
    except LaunchRefused as exc:
        if exc.kind == 'client_missing':
            from plangraph.routing_support.clients import CODEX_REVIEW_CLIENT_MISSING
            stderr.write(runner.redact(str(exc.reason)).replace('\r', ' ').replace('\n', ' ')[:300] + '\n')
            return CODEX_REVIEW_CLIENT_MISSING
        stderr.write(_refusal_line(exc))
        return 2
    except WindowsEnvIncomplete as exc:
        stderr.write(f'launch-spec failed: windows env incomplete ({exc})\n')
        return 1
    except Exception as exc:
        stderr.write(f'launch-spec failed: {type(exc).__name__}\n')
        return 1


if __name__ == '__main__':
    raise SystemExit(fresh_main())
