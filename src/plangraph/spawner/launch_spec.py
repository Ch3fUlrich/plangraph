"""Prepare one harness launch without starting it.

Not done here: leg_probe.before_launch, fall-through to other legs, served-model
witness and nonce bookkeeping (the nonce header is built, not registered), git
config guards, writer no-change baseline, watchdog thresholds, or live.log.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import time

from plangraph.planner.routing import DEFAULT_LEGS, SpawnRefused
from . import context_pack, runner
from .git_guard import writer_launch
from .inline_diff import InlineDiffError
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
        role=lambda v: isinstance(v, str) and v in ('implement', 'review', 'orchestrate'),
        dry_run=lambda v: type(v) is bool,
        leg=lambda v: isinstance(v, dict) and isinstance(v.get('id'), str) and bool(v['id'])
            and ('explicit' not in v or type(v['explicit']) is bool),
        prompt=lambda v: isinstance(v, str), card=lambda v: isinstance(v, dict),
        cwd=lambda v: isinstance(v, str) and os.path.isabs(v) and os.path.isdir(v),
        state=lambda v: isinstance(v, str) and os.path.isabs(v))
    for key, check in checks.items():
        if key not in req or not check(req[key]):
            raise InvalidRequest(key)
    for key in req:
        if key not in checks:
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


def build(request: dict) -> dict:
    _validate(request)
    from .prompt_guard import admit_prompt
    if request['role'] == 'implement':
        admit_prompt(request['prompt'], lambda reason: LaunchRefused('refused', reason))
    from plangraph.planner.legs import SPAWNABLE_LEGS
    leg = next((leg for leg in SPAWNABLE_LEGS if leg.id == request['leg']['id']), None)
    if leg is None:
        raise LaunchRefused('unknown_leg', 'unknown leg id')
    from .target import admit_leg
    from plangraph.routing_errors import SpawnRefused
    try:
        admit_leg(leg)
    except SpawnRefused as exc:
        raise LaunchRefused('leg_inactive', str(exc)) from None
    if request['role'] not in leg.roles:
        raise LaunchRefused('leg_role', f"role {request['role']} not in leg roles")
    service = SpawnService(state=request['state'])
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
        try:
            prompt = service._leg_prompt(request['prompt'], request['card'], request['role'], leg, pack,
                                         document_review=request['leg'].get('explicit') is True)
        except InlineDiffError as exc:
            raise LaunchRefused('inline_diff_failed', str(exc)) from None
        prompt, writer_env = writer_launch(prompt, request['role'], leg.model, request['run_id'])
        try:
            job = asyncio.run(service.runner.spawn(prompt=prompt, model_or_combo=leg.model,
                role=request['role'], dry_run=True, client=leg.harness, extra_env=writer_env,
                prepare_launch=True))  # REMOVE-AT-R10: Rust owns redaction and recording.
        except (SpawnRefused, ValueError) as exc:
            raise LaunchRefused('refused', str(exc)) from None
        for event in job.events:
            if event.get('event') == 'render_refused':
                raise LaunchRefused('render_refused', event['reason'])
        cmd = job.command
        env = service.runner._child_envs[job.job_id]
        argv, events = list(cmd), rec['events'] + job.events
        if not request['dry_run']:
            reason = asyncio.run(host_skip_reason_async(dict(model=leg.model, harness=cmd[0]), env=env))
            if reason:
                raise LaunchRefused('unavailable', reason)
            if admitted_by_render(leg.model):
                events.append(dict(event='admitted_by_render', model=leg.model))
            exe = shutil.which(cmd[0], path=env.get('PATH')) or cmd[0]
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
                    stdin=job.task_prompt if cmd[0] in runner.STDIN_PROMPT_HARNESSES else None,
                    warnings=[w for w in rec['warnings'] if isinstance(w, str)], events=events)
    finally:
        tempfile.tempdir = previous


class WindowsEnvIncomplete(Exception):
    pass


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
        line = f'{exc.kind}: {runner.redact(str(exc.reason))}'
        stderr.write(line.replace('\r', ' ').replace('\n', ' ')[:300] + '\n')
        return 2
    except WindowsEnvIncomplete as exc:
        stderr.write(f'launch-spec failed: windows env incomplete ({exc})\n')
        return 1
    except Exception as exc:
        stderr.write(f'launch-spec failed: {type(exc).__name__}\n')
        return 1
