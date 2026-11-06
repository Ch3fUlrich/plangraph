"""Async job pool for executing and managing spawned agents via OmniRoute ACP.

Features:
  - Async SpawnerRunner managing non-blocking agent executions
  - Tracks active jobs (spawn, status, steer, cancel)
  - Extracts served_model and served_family from response headers or output
  - OmniRoute ACP-first execution with full Windows Tailnet environment preservation
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import sqlite3  # tests patch runner.sqlite3.connect (test_opencode_served_witness)
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from os.path import abspath as _abspath  # tests stub runner.os (test_w36_regressions)
from typing import Any, Mapping, Sequence

from plangraph.spawner.clients import (
    STDIN_PROMPT_HARNESSES,
    build_child_env,
    build_command,
    build_routing_envelope_headers,
)
from plangraph.spawner.claude_env import claude_child_env
from plangraph.spawner.claude_stream import final_text, last_stop_reason, render_review_output
from plangraph.spawner.resolver import MODEL_FAMILIES, detect_model_family
from plangraph.spawner.mimo_policy import check_mimo_path
from plangraph.planner.legs import DEFAULT_LEGS
from plangraph.spawner.winjob import WinJobApi  # re-export: tests patch runner.WinJobApi

_KEEPER_UNCONFIGURED_WARN_S = 600
# D-2966: writers commit in their clone; AutoOS leaf-agent profiles they may read must not fence that.
IMPLEMENT_HEADER = ("You run as the opencode build agent: git commit is allowed and required per your brief; "
                    "AutoOS leaf-agent profiles do not apply to you.")
_keeper_unconfigured_last_warned = None


from .env_redact import first_reason, redact
from .exit_outcome import failure_reason, fallthrough_reason, is_opencode, notify_leg_refused, release_attempts, terminal_reason
from .git_guard import writer_launch
from .proc_kill import KillIncomplete, kill_process_tree, root_identity
from . import proc_kill
from .reap import kill_proven, reap, sample_tree, unproven_survivors
from .served_telemetry import _claude_stream_model, _opencode_served_model, extract_served_telemetry  # re-export: tests import and patch these on runner


def _is_nt() -> bool:
    return os.name == "nt"


def _resolve_launch(
    exe: str,
    argv: Sequence[str] = (),
    *,
    is_nt: bool | None = None,
) -> tuple[str, str | None]:
    """Resolve executable path and enforce Windows launch constraints (I11, I12)."""
    nt = _is_nt() if is_nt is None else is_nt
    if not nt:
        return exe, None

    exe_path = Path(exe)
    ext = exe_path.suffix.lower()
    is_batch = ext in (".cmd", ".bat")

    if is_batch and exe_path.stem.lower() == "opencode":
        sibling = exe_path.parent / "node_modules" / "@opencode" / "cli" / "bin" / "opencode.exe"
        if sibling.is_file():
            exe = str(sibling)
            exe_path = sibling
            ext = exe_path.suffix.lower()
            is_batch = False

    is_opencode = exe_path.stem.lower() == "opencode"
    if not is_opencode:
        limit = 8000 if is_batch else 32000
        cmdline = subprocess.list2cmdline([exe, *argv])
        if len(cmdline) > limit:
            return exe, f"command_line_too_long: {len(cmdline)} chars"

    return exe, None


def is_terminal(status):
    """Only known final run states are terminal; unknown states remain visible."""
    return status in ('completed', 'failed', 'cancelled', 'killed', 'dry_run', 'succeeded', 'timeout', 'refused', 'skipped')


@dataclass
class JobInfo:
    """Execution state of a spawned agent task."""
    job_id: str
    status: str  # "running" | "completed" | "failed" | "cancelled" | "dry_run" | "needs_input"
    task_prompt: str
    model_or_combo: str
    role: str = "implement"
    risk: str = "normal"
    command: list[str] = field(default_factory=list)
    start_time: float = field(default_factory=time.time)
    writer_email: str | None = None
    end_time: float | None = None
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    served_model: str | None = 'unwitnessed'
    served_family: str | None = 'unwitnessed'
    served_witness_reason: str = 'harness supplied no authenticated served-model telemetry'
    request_side_model: str | None = None
    witness_kind: str | None = None
    dry_run: bool = False
    events: list[dict] = field(default_factory=list)
    steer_messages: list[str] = field(default_factory=list)
    cwd: str | None = None
    last_activity_ts: float | None = None
    survivors: list = field(default_factory=list)   # pids that outlived the kill (kill_incomplete)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


#: Every tool key the opencode 2.1.0 config schema knows ('*' exists only in
#: the engine's internal Permission.Ruleset actions): a review child must have
#: all of them denied (D-2905 (1)).
_DENIED_TOOLS = ('bash', 'doom_loop', 'edit', 'external_directory', 'glob', 'grep',
                 'list', 'lsp', 'question', 'read', 'skill', 'task', 'todowrite',
                 'webfetch', 'websearch')


def _opencode_managed_dir(env: Mapping) -> Path:
    """opencode's system (managed) config dir as the child will resolve it.

    Fixed on Linux/macOS; on win32 opencode reads ProgramData from its own
    env, so the CHILD env is read here (the same value the child sees).
    """
    if sys.platform == 'darwin':
        return Path('/Library/Application Support/opencode')
    if sys.platform == 'win32':
        return Path(env.get('ProgramData') or 'C:\\ProgramData', 'opencode')
    return Path('/etc/opencode')

#: The only OPENCODE_* env names a review child may carry. Anything else a
#: caller sets (e.g. OPENCODE_CLI_CONFIG_CONTENT, OPENCODE_TEST_HOME) is
#: dropped before launch so no caller-set override can re-enable a tool or add
#: a config layer (REVIEW-WITNESS-OPENCODE R1).
_REVIEW_OPENCODE_ENV_ALLOW = ('OPENCODE_CONFIG_CONTENT', 'OPENCODE_CONFIG',
                              'OPENCODE_CONFIG_DIR', 'OPENCODE_DISABLE_PROJECT_CONFIG',
                              'OPENCODE_DISABLE_CLAUDE_CODE', 'OPENCODE_DISABLE_CLAUDE_CODE_SKILLS',
                              'OPENCODE_DISABLE_AUTOUPDATE', 'OPENCODE_API_KEY')


def _opencode_tools_denied(config_json, env) -> bool:
    """True only when the child's final config AND env prove every tool denied.

    Checked, all required: 'permission' maps '*' and every _DENIED_TOOLS key
    to 'deny'; the managed config dir (_opencode_managed_dir, resolved from
    the child env) holds no opencode.json/opencode.jsonc; 'mcp' is {} and
    'plugin' is [] in config_json; HOME,
    XDG_CONFIG_HOME, XDG_DATA_HOME and OPENCODE_CONFIG_DIR all resolve inside
    the same per-job dir (XDG_DATA_HOME); OPENCODE_CONFIG is an existing file
    inside that dir whose content parses to {};
    OPENCODE_DISABLE_PROJECT_CONFIG, OPENCODE_DISABLE_CLAUDE_CODE and
    OPENCODE_DISABLE_CLAUDE_CODE_SKILLS are each '1'; and no OPENCODE_* env
    name outside _REVIEW_OPENCODE_ENV_ALLOW is present. This proves only the
    env/config shape: the runtime behaviour of opencode is not proven by this
    function (an L1 runtime probe covers that).
    """
    try:
        cfg = json.loads(config_json or '{}')
    except (ValueError, TypeError):
        return False
    if not isinstance(cfg, dict):
        return False
    permission = cfg.get('permission')
    if not isinstance(permission, dict) or not all(
            permission.get(tool) == 'deny' for tool in ('*',) + _DENIED_TOOLS):
        return False
    if cfg.get('mcp') != {} or cfg.get('plugin') != []:
        return False
    if not isinstance(env, Mapping):
        return False
    managed = _opencode_managed_dir(env)
    if any((managed / name).exists() for name in ('opencode.json', 'opencode.jsonc')):
        return False
    for name in env:
        if str(name).startswith('OPENCODE_') and name not in _REVIEW_OPENCODE_ENV_ALLOW:
            return False
    if (env.get('OPENCODE_DISABLE_PROJECT_CONFIG') != '1'
            or env.get('OPENCODE_DISABLE_CLAUDE_CODE') != '1'
            or env.get('OPENCODE_DISABLE_CLAUDE_CODE_SKILLS') != '1'):
        return False
    try:
        job_dir = Path(env.get('XDG_DATA_HOME') or '')
        if not job_dir.name:
            return False
        for key in ('HOME', 'XDG_CONFIG_HOME', 'OPENCODE_CONFIG_DIR'):
            path = Path(env.get(key) or '')
            if not path.name or not path.resolve().is_relative_to(job_dir.resolve()):
                return False
        cfg_path = Path(env.get('OPENCODE_CONFIG') or '')
        return (bool(cfg_path.name) and cfg_path.is_file()
                and cfg_path.resolve().is_relative_to(job_dir.resolve())
                and json.loads(cfg_path.read_text()) == {})
    except (OSError, ValueError):
        return False


class NonceCapacity(RuntimeError):
    """All nonce slots belong to live runs."""


class SpawnerRunner:
    """Async process runner for Plangraph agent tasks."""

    identity_provider = staticmethod(root_identity)   # ownership seam: tests inject a fake provider

    def __init__(self, default_cwd: str | None = None, *, process_factory=None, tree_killer=None):
        self.default_cwd = default_cwd or os.getcwd()
        self.jobs: dict[str, JobInfo] = {}
        self.on_sample = None
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self.process_factory = process_factory or asyncio.create_subprocess_exec
        self.tree_killer = tree_killer or self._own_kill
        self.platform = None   # tests inject 'nt'
        self.provider_responses = {}
        self._nonces = {}
        self._adapter_tokens = {}
        self._child_envs = {}
        self._ocdata_dirs = {}
        self._reaped_jobs = set()
        self._git_guards = {}
        self._stdin_payloads = {}

    def served_witness(self, job_id, model, *, adapter_token, finish_reason=None):
        """Record transport telemetry authenticated by an in-process adapter.

        Do not expose this capability to the child: even a child that knows its
        run nonce cannot attest to its own serving identity. Only a real
        admitted model id is attestable: a combo alias such as "l2-worker"
        (no "/") or any id outside the canonical families would otherwise
        publish the label itself as served_family. A refused witness leaves
        the job unwitnessed.
        """
        if adapter_token is None or adapter_token is not self._adapter_tokens.get(job_id):
            raise ValueError('invalid adapter token')
        if not isinstance(model, str) or not model.strip():
            raise ValueError('missing served model')
        model = model.strip()
        family = detect_model_family(model)
        if '/' not in model or family not in MODEL_FAMILIES:
            raise ValueError('served model is not an admitted model id: ' + model)
        job = self.jobs[job_id]
        job.served_model = model
        job.served_family = family
        job.served_witness_reason = 'authenticated gateway adapter telemetry'
        job.response_finish_reason = finish_reason  # final witness telemetry, never inherited

    def provider_response(self, job_id, leg, status, body, headers, *, source=None,
                          nonce=None, adapter_token=None, account=None, combo=None, store=None):
        """Consume trusted gateway transport evidence, never free-text output.

        Public calls are restricted to trusted gateway adapters. The nonce-checked
        stderr envelope parser is the only entry point for child-supplied data.
        Exit codes alone are not evidence.
        """
        from plangraph.spawner import leg_state

        if job_id not in self.jobs:
            raise KeyError(job_id)
        if source == 'stderr_envelope':
            if not nonce or nonce != self._nonces.get(job_id):
                raise ValueError('invalid run nonce')
        elif source == 'gateway_adapter':
            if adapter_token is None or adapter_token is not self._adapter_tokens.get(job_id):
                raise ValueError('invalid adapter token')
        else:
            raise ValueError('unknown provider response source')
        target = store or leg_state
        if '/' not in leg:
            leg = 'agy/' + leg
        if status in (429, 402):
            target.record_provider_response(leg, status, body, headers, "runner:gateway-response", account=account, combo=combo)
            from plangraph.spawner.leg_state import _redact
            self.provider_responses[job_id] = redact({"status": status, "body": _redact(body), "headers": dict(headers or {})})
        elif 200 <= status < 300:
            target.record_probe_pass(leg, caller="runner:gateway-response", account=account, combo=combo)

    async def spawn(
        self,
        prompt: str,
        model_or_combo: str = "mimo/mimo-v2.6-flash",
        role: str = "implement",
        risk: str = "normal",
        headers: Mapping[str, str] | None = None,
        dry_run: bool = False,
        cwd: str | None = None,
        extra_env: Mapping[str, str | None] | None = None,
        gateway: bool = True,
        gateway_online: bool = True,
        wait_seconds: float = 0.0,
        client: str | None = None,
        watchdog=None, live_log=None, prompt_record=None, prepare_launch=False, wake_variant='initial',  # REMOVE-AT-R10
    ) -> JobInfo:
        """Spawn a new agent job and track its state."""
        from plangraph.planner.routing import validate_model
        validate_model(model_or_combo)
        if role == "implement" and not prompt.startswith(IMPLEMENT_HEADER):
            prompt = IMPLEMENT_HEADER + "\n\n" + prompt
        # REMOVE-AT-R10: capture the final prompt before command construction.
        if prepare_launch and not dry_run:  # REMOVE-AT-R10
            raise ValueError('launch preparation requires dry run')
        prompt = redact(prompt)  # REMOVE-AT-R10
        if not prepare_launch and prompt_record is not None:  # REMOVE-AT-R10
            from .private_io import atomic_write_private
            path, record, save = prompt_record
            if not dry_run:  # REMOVE-AT-R10
                atomic_write_private(path, prompt)
            record.update(prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(), wake_variant=wake_variant)
            if not dry_run:  # REMOVE-AT-R10
                save(record)
        timeout = os.environ.get('PLANGRAPH_SPAWN_RUN_TIMEOUT')
        if timeout:
            try:
                seconds = float(timeout)
                if not math.isfinite(seconds) or seconds <= 0:
                    raise ValueError
            except ValueError:
                raise ValueError('invalid run timeout: PLANGRAPH_SPAWN_RUN_TIMEOUT') from None
        job_id = f"job-{uuid.uuid4().hex[:8]}"
        work_dir = cwd or self.default_cwd

        import secrets
        nonce = secrets.token_hex(32)
        if len(self._nonces) >= 4096:
            for key in list(self._nonces):
                if key not in self.jobs:
                    self._nonces.pop(key)
            finished = sorted(
                (key for key in self._nonces if key in self.jobs
                 and self.jobs[key].status != 'running'),
                key=lambda key: self.jobs[key].end_time or self.jobs[key].start_time,
            )
            for key in finished if len(self._nonces) >= 4096 else ():
                self._nonces.pop(key, None)
                if len(self._nonces) < 4096:
                    break
            if len(self._nonces) >= 4096:
                raise NonceCapacity('active run nonce capacity reached')
        # Build routing envelope headers if not provided
        envelope_headers = dict(headers or {})
        if not envelope_headers:
            envelope_headers = build_routing_envelope_headers(
                role=role,
                risk=risk,
                min_intelligence=44.0,
                required_context=32000,
            )

        envelope_headers["X-OmniRoute-Run-Nonce"] = build_routing_envelope_headers(nonce=nonce)["X-OmniRoute-Run-Nonce"]

        cmd = build_command(
            prompt=prompt,
            model_or_combo=model_or_combo,
            role=role,
            risk=risk,
            gateway=gateway,
            gateway_online=gateway_online,
            headers=envelope_headers,
            client=client,
        )

        child_env = build_child_env(extra_env, headers=envelope_headers)
        # D-3505: harnesses take their project dir from an inherited $PWD over the
        # process cwd; a caller's PWD put probe commits into the caller's worktree.
        child_env['PWD'] = _abspath(work_dir)
        if cmd[0] == 'claude':   # D-3605: subscription login only; D-3649: an allowlisted env, never a scrubbed copy
            child_env = claude_child_env(child_env)
        render_reason = None
        tools_event = None
        if cmd[0] == 'opencode':
            # Name the session so the monitor can witness the served model
            # (D-2898). --title goes right after the --model pair so it always
            # precedes any positional/prompt argument ('opencode run' takes
            # [<message...>]); the prompt itself rides on stdin.
            try:
                title_at = cmd.index('--model') + 2
            except ValueError:
                title_at = len(cmd)  # no positional exists on that path
            cmd[title_at:title_at] = ['--title', f'plangraph-{job_id}']
            from .opencode_overlay import overlay_env
            child_env = overlay_env(child_env)
            from plangraph.routing_support.opencode_render import render_gateway_providers
            render_reason = render_gateway_providers(child_env, model_or_combo)
            if render_reason is None:
                # Overlay only copies an env name through when the parent already
                # holds it; hand the child the host keys from the AutoOS keys file
                # so mimo and opencode/*-free runs can authenticate (D-2759).
                from plangraph.routing_support.host_keys import GATEWAY_KEY_ENV, add_host_keys
                add_host_keys(child_env, model_or_combo,
                    needs_gateway=GATEWAY_KEY_ENV in (child_env.get('OPENCODE_CONFIG_CONTENT') or ''))
            # D-2921 F1 / D-3236: every role (writers too) runs without the global, project and
            # Claude Code config layers -- a global `instructions` entry made a writer change nothing.
            from .opencode_isolation import close_config_layers
            oc_dir = self._ocdata_dirs[job_id] = close_config_layers(child_env)
            if role == 'review':
                # D-2905 (1): the per-job dir is also the spawner-owned store, read only after
                # child exit -- the shared store is child-writable and proves nothing.
                child_env['XDG_DATA_HOME'] = oc_dir
                # REVIEW-WITNESS-OPENCODE R1: the child gets NO inherited
                # config layer at all -- HOME and XDG_CONFIG_HOME live in the
                # per-job dir and every caller-set OPENCODE_* override outside
                # _REVIEW_OPENCODE_ENV_ALLOW is dropped.
                child_env['HOME'] = oc_dir
                xdg_config = os.path.join(oc_dir, 'config')
                os.mkdir(xdg_config, 0o700)
                child_env['XDG_CONFIG_HOME'] = xdg_config
                for name in [n for n in child_env
                             if str(n).startswith('OPENCODE_') and n not in _REVIEW_OPENCODE_ENV_ALLOW]:
                    del child_env[name]
                # D-2905 (1): deny every tool for the review seat, then record
                # the spawner's own check of the FINAL env value.
                try:
                    cfg = json.loads(child_env.get('OPENCODE_CONFIG_CONTENT') or '{}')
                    if not isinstance(cfg, dict):
                        raise ValueError('not an object')
                    cfg['permission'] = {tool: 'deny' for tool in ('*',) + _DENIED_TOOLS}
                    cfg.update(mcp={}, plugin=[])   # D-2921 F1: drop every MCP server and plugin
                    from .gateway_witness import tag_session
                    tag_session(cfg, job_id)   # D-3226: the gateway logs this run's sessionTag
                    child_env['OPENCODE_CONFIG_CONTENT'] = json.dumps(cfg, sort_keys=True)
                except (ValueError, TypeError):
                    pass  # unparseable config cannot be proven denied below
                tools_event = {'event': 'opencode_tools_denied' if _opencode_tools_denied(
                    child_env.get('OPENCODE_CONFIG_CONTENT') or '', child_env) else 'opencode_tools_not_denied'}
        else:
            child_env.pop('OPENCODE_CONFIG_CONTENT', None)
        if render_reason is None:
            self._child_envs[job_id] = child_env

        try:
            if dry_run:
                served_model = model_or_combo
                served_family = detect_model_family(model_or_combo)
                if "/" not in model_or_combo and not any(l.model == model_or_combo for l in DEFAULT_LEGS):
                    from plangraph.spawner.resolver import get_catalog_models, catalog_row_admitted
                    combo_legs = [
                        m for m in get_catalog_models()
                        if model_or_combo in (m.get("combos") or []) and catalog_row_admitted(m)
                    ]
                    if combo_legs:
                        served_model = combo_legs[0]["id"]
                        served_family = detect_model_family(served_model)
                    else:
                        from plangraph.routing_errors import SpawnRefused
                        raise SpawnRefused('no admitted legs for combo: ' + model_or_combo)
                stdin_note = (
                    f" [stdin: {len(prompt.encode('utf-8'))} bytes]"
                    if cmd[0] in STDIN_PROMPT_HARNESSES
                    else ""
                )
                job = JobInfo(
                    job_id=job_id,
                    status="dry_run",
                    task_prompt=prompt,
                    model_or_combo=model_or_combo,
                    role=role,
                    risk=risk,
                    command=cmd,
                    end_time=time.time(),
                    exit_code=0,
                    stdout=f"[dry-run] Planned command: {' '.join(cmd)}{stdin_note}",
                    served_model=served_model,
                    served_family=served_family,
                    dry_run=True,
                    cwd=work_dir,
                )
                if render_reason:
                    job.events.append({'event': 'render_refused', 'reason': render_reason})
                if tools_event:
                    job.events.append(tools_event)
                self.jobs[job_id] = job
                job.prompt_sha256 = hashlib.sha256(prompt.encode()).hexdigest()
                return job

            job = JobInfo(
                job_id=job_id,
                status="running",
                task_prompt=prompt,
                model_or_combo=model_or_combo,
                role=role,
                risk=risk,
                command=cmd,
                dry_run=False,
                cwd=work_dir,
                writer_email=child_env.get('GIT_AUTHOR_EMAIL'),
            )
            from .probe_cleanup import register
            self.jobs[job_id] = job; register(job_id)
            job.prompt_sha256 = hashlib.sha256(prompt.encode()).hexdigest()
            from .run_watchdog import thresholds, edit_signature
            job._watchdog_limits = watchdog or thresholds(role=role)
            job._live_log = live_log
            job._watchdog_baseline = await asyncio.to_thread(edit_signature, work_dir)
            if tools_event:
                job.events.append(tools_event)

            if render_reason:
                job.status = 'failed'
                job.exit_code = 1
                job.stderr = render_reason
                job.end_time = time.time()
                job.events.append({'event': 'render_refused', 'reason': render_reason})
                return job

            from .resolver import host_skip_reason_async
            unavailable = await host_skip_reason_async(dict(model=model_or_combo, harness=cmd[0]), env=child_env)
            if unavailable:
                job.status = 'failed'
                job.exit_code = 1
                job.stderr = unavailable
                job.end_time = time.time()
                return job
            from .resolver import admitted_by_render
            if admitted_by_render(model_or_combo):  # trace a later launch failure to D-2869
                job.events.append({'event': 'admitted_by_render', 'model': model_or_combo})

            import shutil
            resolved_exe = shutil.which(cmd[0], path=child_env.get("PATH")) or cmd[0]
            resolved_exe, launch_refusal = _resolve_launch(resolved_exe, cmd[1:], is_nt=_is_nt())
            if launch_refusal:
                job.status = 'failed'
                job.exit_code = 1
                job.stderr = launch_refusal
                job.end_time = time.time()
                return job

            if cmd[0] in STDIN_PROMPT_HARNESSES:
                self._stdin_payloads[job_id] = prompt.encode("utf-8")
            self._adapter_tokens[job_id] = object()
            if role in ("implement", "fix"):
                guard = self._init_git_config_guard(work_dir)
                if guard is not None:
                    self._git_guards[job_id] = guard
                from .opencode_isolation import record_baseline
                await asyncio.to_thread(record_baseline, job, work_dir, list(self.jobs.values()))   # D-3236
            try:
                proc = await self.process_factory(
                    resolved_exe,
                    *cmd[1:],
                    cwd=work_dir,
                    env=child_env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    stdin=(
                        asyncio.subprocess.PIPE
                        if cmd[0] in STDIN_PROMPT_HARNESSES
                        else asyncio.subprocess.DEVNULL
                    ),
                    **({"start_new_session": True} if os.name != "nt" else {}),
                )
            except Exception as exc:
                self._adapter_tokens.pop(job_id, None)
                job.status = 'failed'
                job.exit_code = 1
                job.end_time = time.time()
                job.events.append({'event': 'spawn_failed', 'error': type(exc).__name__})
                raise
            self._processes[job_id] = proc
            self._nonces[job_id] = nonce
            root_pid = getattr(proc, 'pid', None)   # pid-less process objects (test fakes): nothing of ours to track or kill
            if root_pid is not None:
                self._owned = {**getattr(self, '_owned', {}), job_id: dict(pid=root_pid, pid_start=self.identity_provider(root_pid, platform='nt' if self._nt() else None), job_object=False)}
                start = self._owned[job_id]['pid_start']
                if start is not None and not self._nt():   # setsid() orphans: remember the tree while the root lives
                    self._seen = {**getattr(self, '_seen', {}), job_id: {}}
                    self._samplers = {**getattr(self, '_samplers', {}), job_id: asyncio.create_task(sample_tree(self._seen[job_id], root_pid, start))}

            # F3: kill-on-close Job Object (SPAWN-ORPHAN-REAP); fail closed if unavailable.
            if root_pid is not None and self._nt():
                self._win_jobs = getattr(self, '_win_jobs', {})
                try:
                    self.win_job_api = getattr(self, 'win_job_api', None) or WinJobApi()
                    handle = self.win_job_api.create_job()
                    try:
                        self.win_job_api.assign(handle, root_pid)
                    except Exception:
                        self.win_job_api.close(handle)
                        raise
                    self._owned[job_id]['job_object'] = self._win_jobs[job_id] = handle
                    # Descendants made before assign() are not job members: record them now, kill+verify at reap.
                    self._escapees = {**getattr(self, '_escapees', {}), job_id: proc_kill.escapees(root_pid, self.win_job_api.pids(handle) if hasattr(type(self.win_job_api), 'pids') else [])}
                    self._owned[job_id].update(job_name=getattr(self.win_job_api, 'name', None), escapees=list(self._escapees[job_id]))
                except Exception:
                    stale = self._win_jobs.pop(job_id, None)
                    if stale is not None:   # assigned but unverifiable: release the handle, the run does not start
                        try:
                            self.win_job_api.close(stale)
                        except Exception:
                            pass
                    job.events.append({'event': 'win_job_unavailable'})
                    await self._reap_process(job_id, proc)
                    job.status, job.exit_code, job.end_time = 'failed', 1, time.time()
                    job.survivors = job.survivors or [root_pid]
                    return job

            # Start background monitor
            task = asyncio.create_task(self._monitor_process(job_id, proc, model_or_combo))
            self._tasks[job_id] = task

            if wait_seconds > 0:
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=wait_seconds)
                except asyncio.TimeoutError:
                    pass

            return self.jobs[job_id]
        finally:
            # A running monitor owns the environment until output is scrubbed.
            if job_id not in self._tasks:
                self._child_envs.pop(job_id, None)
                self._git_guards.pop(job_id, None)
                self._stdin_payloads.pop(job_id, None)
                oc_dir = self._ocdata_dirs.pop(job_id, None)
                if oc_dir is not None:
                    import shutil
                    shutil.rmtree(oc_dir, ignore_errors=True)
            else:
                # D-2921 F3: a monitor cancelled before its first step never
                # runs its own finally; sweep its store dir once it settles
                # (no-op when the monitor already cleaned up).
                def _sweep(_task, _self=self, _jid=job_id):
                    _oc = _self._ocdata_dirs.pop(_jid, None)
                    if _oc is not None:
                        import shutil
                        shutil.rmtree(_oc, ignore_errors=True)
                self._tasks[job_id].add_done_callback(_sweep)

    def _nt(self) -> bool:
        return self.platform == 'nt' or _is_nt()

    async def kill_recorded(self, pid, pid_start=None, job_name=None, escapees=()):
        """Cancel after a service restart: prove the recorded root by its persisted start token,
        kill its tree, verify.  Returns survivors ([] = clean); unprovable and alive = survivors."""
        plat = 'nt' if self._nt() else None
        if plat == 'nt' and type(pid_start) is int:
            from .win_recorded import kill
            return await kill(pid, pid_start, job_name, escapees)
        if pid_start is None or plat == 'nt':
            return [pid] if plat == 'nt' else unproven_survivors(pid, plat)
        return await kill_proven(self, pid, pid_start, plat=plat) or []

    async def _own_kill(self, pid, ident=None):
        """Default killer: posix signals only a group proven ours by (pid, starttime); nt kills via the Job Object."""
        if self._nt():
            return
        if ident is None:   # match on (pid, current starttime), never on pid alone
            st = proc_kill._stat(pid)
            ident = next((o['pid_start'] for o in getattr(self, '_owned', {}).values()
                          if o.get('pid') == pid and st and o.get('pid_start') == st[4]), None)
        await kill_process_tree(pid, ident=ident)

    async def _reap_process(self, job_id: str, proc: Any) -> None:
        reaped = getattr(self, '_reaped_jobs', None)
        if reaped is None:
            self._reaped_jobs = set()
            reaped = self._reaped_jobs
        if job_id in reaped:
            return
        reaped.add(job_id)
        await reap(self, job_id, proc)

    async def _monitor_process(
        self,
        job_id: str,
        proc: asyncio.subprocess.Process,
        requested_model: str,
    ) -> None:
        """Monitor process execution until completion."""
        try:
            from .run_watchdog import collect
            stdout_bytes, stderr_bytes = await collect(self, job_id, proc)
            stdout_text = stdout_bytes.decode("utf-8", errors="replace")
            stderr_text = stderr_bytes.decode("utf-8", errors="replace")
            exit_code = self.jobs[job_id].exit_code if (self.jobs[job_id].status in ('stalled', 'timeout', 'cancelled')
                                                        or self.jobs[job_id].survivors
                                                        or getattr(self.jobs[job_id], 'refusal', None)) else proc.returncode

            job = self.jobs[job_id]
            # Only the fork's exact stderr transport envelope is evidence.
            for line in stderr_text.splitlines():
                try:
                    envelope = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(envelope, dict) or set(envelope) != {'autoos_provider_response'}:
                    continue
                response = envelope['autoos_provider_response']
                if not isinstance(response, dict) or response.get('nonce') != self._nonces.get(job_id) or not self._nonces.get(job_id):
                    job.events.append({'event': 'forged_envelope'})
                    continue
                response = {k: v for k, v in response.items() if k != 'nonce'}
                if (isinstance(response, dict) and set(response) == {'leg', 'status', 'body', 'headers'}
                        and isinstance(response['leg'], str) and response['leg']
                        and type(response['status']) is int and response['status'] in (429, 402)
                        and isinstance(response['body'], str) and response['body'].strip()
                        and isinstance(response['headers'], dict)
                        and all(isinstance(k, str) and isinstance(v, str) for k, v in response['headers'].items())):
                    self.provider_response(job_id, **response, source='stderr_envelope',
                                           nonce=self._nonces[job_id])
            from .env_redact import redact_env_values
            env = self._child_envs.get(job_id, os.environ)
            job.stdout = redact(redact_env_values(stdout_text, env))
            job.stderr = redact(redact_env_values(stderr_text, env))
            job.exit_code = exit_code
            job.end_time = time.time()
            if job.status not in ('stalled', 'timeout', 'cancelled'):
                # No-tools review seats can emit a complete verdict before rc 1 termination.
                lines = stdout_text.rstrip('\r\n').splitlines()
                review_verdict = (
                    exit_code == 1 and is_opencode(job) and job.role == 'review'
                    and any(event.get('event') == 'opencode_tools_denied' for event in job.events)
                    and bool(lines) and lines[-1] in ('VERDICT: ACCEPT', 'VERDICT: REQUEST_CHANGES')
                )
                job.status = "completed" if exit_code == 0 or review_verdict else "failed"
            if job.status == 'failed' and is_opencode(job):
                job.events.append(dict(event='harness_failed', error=failure_reason(job)))
            from .opencode_isolation import fail_unchanged_writer
            from .question_bridge import question_exit
            if not question_exit(job, {}):
                await asyncio.to_thread(fail_unchanged_writer, job, job.cwd)   # D-3236: no change = failed

            # D-2905 (2): the child-writable session store is request-side only ('request-side-opencode-store',
            # never 'served'); read only for a review leg's own store, all tools denied, exit 0 (D-2905 (1)).
            if job.command and job.command[0] == 'opencode' and job.role == 'review':
                oc_dir = self._ocdata_dirs.get(job_id) if exit_code == 0 else None
                denied = _opencode_tools_denied(
                    env.get('OPENCODE_CONFIG_CONTENT') or '', env)
                # R2: the store read may block up to its 2s sqlite timeout on a
                # locked db; a thread keeps the monitor loop responsive.
                m = (await asyncio.to_thread(_opencode_served_model,
                                             Path(oc_dir) / 'opencode' / 'opencode.db',
                                             f'plangraph-{job_id}')
                     if oc_dir and denied else None)
                if m is not None and m == requested_model:
                    job.request_side_model = m
                    job.witness_kind = 'request-side-opencode-store'
                elif oc_dir and denied:
                    job.events.append({'event': 'opencode_witness_unavailable'})
                if denied:   # D-3226 response-side, any exit code: the gateway call log names the model
                    from .gateway_witness import witness
                    await asyncio.to_thread(witness, self, job_id, job, env)

            if job.command and job.command[0] == 'claude' and last_stop_reason(stdout_text) == 'refusal':
                answer = final_text(stdout_text)
                job.events.append(dict(event='model_refusal', stop_reason='refusal',
                                       refusal_text_chars=len(answer[0]) if answer else 0))
                job.status = 'failed'
            if job.command and job.command[0] == 'claude' and exit_code == 0:   # D-3605
                m = _claude_stream_model(stdout_text)
                if m is not None and m == requested_model:
                    job.request_side_model = m
                    job.witness_kind = 'request-side-claude-stream'
                # D-3649: the review text (VERDICT line) is the stream's final result, not the raw stream.
                await asyncio.to_thread(render_review_output, job, stdout_text,
                                        lambda text: redact(redact_env_values(text, env)))

            # Free text (even provider-like headers) is child-controlled; no adapter witness stays explicit.
            job.served_model = job.served_model or 'unwitnessed'
            job.served_family = job.served_family or 'unwitnessed'
        except asyncio.CancelledError:
            await self._reap_process(job_id, proc)
            if job_id in self.jobs:
                from .cancel_job import publish_cancel; publish_cancel(self.jobs[job_id])
                self.jobs[job_id].end_time = time.time()
        except Exception as exc:
            job = self.jobs[job_id]
            job.status = 'failed'
            job.exit_code = 1
            job.end_time = time.time()
            job.events.append({'event': 'monitor_failed', 'error': type(exc).__name__})
            logging.getLogger(__name__).exception('monitor_failed: %s', type(exc).__name__)
        finally:
            try:
                await self._reap_process(job_id, proc)
            except Exception:
                pass
            self._check_git_config_guard(job_id)
            self._git_guards.pop(job_id, None)
            self._stdin_payloads.pop(job_id, None)
            self._child_envs.pop(job_id, None)
            self._adapter_tokens.pop(job_id, None)
            self._nonces.pop(job_id, None)
            self._processes.pop(job_id, None)
            oc_dir = self._ocdata_dirs.pop(job_id, None)
            if oc_dir is not None:
                import shutil
                shutil.rmtree(oc_dir, ignore_errors=True)
            getattr(self, '_reaped_jobs', set()).discard(job_id)

    @staticmethod
    def _init_git_config_guard(work_dir):
        from .git_guard import init_guard
        return init_guard(work_dir)

    def _check_git_config_guard(self, job_id):
        from .git_guard import check_guard
        check_guard(self._git_guards.get(job_id), self.jobs.get(job_id))

    async def status(self, job_id: str | None = None) -> Any:
        """Query status of a specific job or all jobs."""
        if job_id is not None and not isinstance(job_id, str):
            from plangraph.routing_errors import ParameterRefused
            raise ParameterRefused('job_id must be a string')
        if job_id:
            if job_id not in self.jobs:
                raise KeyError(f"Job {job_id} not found")
            return self.jobs[job_id]
        return [job for job in self.jobs.values()]

    async def steer(self, job_id: str, message: str) -> dict[str, Any]:
        """Send steer instruction to an active job.

        Note: for opencode, stdin is closed after the prompt is sent, so steer
        will report ``{'ok': False, 'steered': False, 'error': 'stdin closing'}``.
        """
        if not isinstance(job_id, str):
            from plangraph.routing_errors import ParameterRefused
            raise ParameterRefused('job_id must be a string')
        if job_id not in self.jobs:
            return {'ok': False, 'steered': False, 'error': 'unknown job'}
        job = self.jobs[job_id]
        # Audit attempted instructions; success is reported separately.
        message = redact(message)
        job.steer_messages.append(message)

        proc = self._processes.get(job_id)
        if not proc or getattr(proc, 'stdin', None) is None:
            return {'ok': False, 'steered': False, 'error': 'no stdin'}
        if proc.stdin.is_closing():
            return {'ok': False, 'steered': False, 'error': 'stdin closing'}
        if proc.stdin is not None:
            try:
                proc.stdin.write((message + "\n").encode("utf-8"))
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                job.events.append({"event": "steer_failed", "error": type(exc).__name__})
                return {'ok': False, 'steered': False, 'error': type(exc).__name__}
            except Exception as exc:
                job.events.append({"event": "steer_failed", "error": type(exc).__name__})
                return {'ok': False, 'steered': False, 'error': type(exc).__name__}

        return {'ok': True, 'steered': True, 'error': None}

    async def cancel(self, job_id: str) -> dict[str, Any]:
        """Cancel a running job."""
        from .cancel_job import cancel_job
        return await cancel_job(self, job_id)


def spawn_service(*args, **kwargs):
    """Shared hardened dispatch for the public CLI and MCP entry points."""
    from .service import SpawnService
    from dataclasses import asdict
    from pathlib import Path

    class DispatchService(SpawnService):
        async def _continue_writer(self, record, output):
            if record.get('status') != 'needs_input':
                return await super()._continue_writer(record, output)

        def __init__(self, *args, completion_sink=None, keeper_sink=None, **kwargs):
            super().__init__(*args, **kwargs)
            from .notify import FileInbox
            self.completion_sink = completion_sink or FileInbox(self.state)
            from .notify import KeeperSink
            self.keeper_sink = keeper_sink or KeeperSink()
            self._running_records = {}
            if hasattr(self.runner, '__dict__'):
                self.runner.on_sample = lambda jid: self._save(self._running_records[jid]) if jid in self._running_records else None

        async def cancel(self, run_id):   # D-3505: the leg loop of the supervisor stops on this marker
            self._path(run_id).parent.exists() and self._path(run_id).with_name('cancelled').touch()
            return await super().cancel(run_id)

        def _save(self, record, *, retry=False):
            import socket
            record.setdefault('host', os.environ.get('PLANGRAPH_HOST_NAME') or socket.gethostname())
            from .notify import completion_event, CommandSink, KeeperNotConfigured, KeeperSink
            job = getattr(self.runner, 'jobs', {}).get(record.get('job_id'))
            if job is not None:
                record['last_activity_ts'] = getattr(job, 'last_activity_ts', None)
                for stream in ('stdout', 'stderr'):
                    record[stream + '_tail'] = getattr(job, stream, '')
                for attempt in record.get('attempts', []):
                    if attempt.get('status') in ('failed', 'skipped') and not attempt.get('skip_reason'):
                        attempt['skip_reason'] = redact(failure_reason(job))
            terminal = record.get('ended') is not None and record.get('status') != 'running'
            record.setdefault('wait_cmd', 'plangraph-spawn wait ' + record['run_id'])
            if not retry:
                super()._save(record)
            changed = False
            if terminal:
                if 'completion' not in record:
                    record['completion'] = completion_event(record, record.get('cwd') or getattr(self.runner, 'default_cwd', os.getcwd()))
                    if record.get('status') == 'needs_input':
                        record['completion'].update(event='NEEDS_INPUT', question=record['question'])
                    changed = True
            if changed:
                super()._save(record)
            if terminal:
                for recipient, sink in (('parent', self.completion_sink), ('keeper', self.keeper_sink)):
                    marker = self._path(record['run_id']).parent / (recipient + '.delivered')
                    if marker.exists():
                        continue
                    try:
                        no_keeper_item = (recipient == 'keeper' and isinstance(sink, KeeperSink)
                                          and sink.url and sink.token_file and not record['completion'].get('item'))
                        if not no_keeper_item:
                            sink.deliver(recipient, record['completion'])
                        from .private_io import atomic_write_private
                        atomic_write_private(marker, 'delivered\n')
                    except KeeperNotConfigured:
                        global _keeper_unconfigured_last_warned
                        now = time.monotonic()
                        last = _keeper_unconfigured_last_warned
                        if last is None or (now - last) >= _KEEPER_UNCONFIGURED_WARN_S:
                            _keeper_unconfigured_last_warned = now
                            logging.getLogger(__name__).warning('completion delivery skipped: keeper not configured (queued)')
                    except Exception:
                        logging.getLogger(__name__).exception('completion delivery failed: %s', recipient)
                command = os.environ.get('PLANGRAPH_SPAWN_NOTIFY_CMD')
                if command:
                    for recipient in ('parent', 'keeper'):
                        marker = self._path(record['run_id']).parent / (recipient + '.hook.delivered')
                        if not marker.exists():
                            try:
                                CommandSink(command).deliver(recipient, record['completion'])
                                from .private_io import atomic_write_private
                                atomic_write_private(marker, 'delivered\n')
                            except Exception:
                                logging.getLogger(__name__).exception('completion hook failed')

        def retry_completions(self):
            """Re-drive durable terminal records after a supervisor/bridge outage."""
            for record in self.ps()['runs']:
                recipients = ['parent', 'keeper']
                markers = [r + '.delivered' for r in recipients]
                if os.environ.get('PLANGRAPH_SPAWN_NOTIFY_CMD'):
                    markers += [r + '.hook.delivered' for r in recipients]
                pending = 'completion' not in record or any(
                    not (self._path(record['run_id']).parent / marker).exists()
                    for marker in markers)
                if record.get('ended') is not None and record.get('status') != 'running' and pending:
                    try:
                        self._save(record, retry=True)
                    except Exception:
                        logging.getLogger(__name__).exception('completion retry failed')

        async def wait(self, run_id, timeout=None):
            if timeout is not None and timeout < 0:
                raise ValueError('timeout must be nonnegative')
            deadline = None if timeout is None else time.monotonic() + timeout
            while True:
                record = self.result(run_id)
                if record.get('ended') is not None and record['status'] != 'running':
                    return record
                from .exit_outcome import orphaned
                gone = orphaned(record, time.time())
                if gone is not None:
                    self._save(gone)
                    return gone
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError('run wait timed out: ' + run_id)
                await asyncio.sleep(.02)

        def _route(self, card, role='implement', writer_families=(), review_of=None, **options):
            from . import leg_state
            reader = getattr(self.leg_reader, 'leg_status', self.leg_reader) or leg_state.leg_status
            original = self.leg_reader
            from .leg_probe import read_status
            self.leg_reader = lambda model, **kw: read_status(reader, model, **kw)
            try:
                return super()._route(card, role, writer_families, review_of, **options)
            finally:
                self.leg_reader = original

        async def _spawn(self, card=None, prompt='', role='implement', writer_families=(),
                        review_of=None, model=None, leg=None, override_reason=None,
                        dry_run=False, wait_seconds=0, no_fallthrough=False, benchmark=None, require_bench=False, review_scope=None,
                        seats=False, _plan=None):
            from .private_io import SAVE_READ_ATTEMPTS
            from .run_watchdog import terminal_status, thresholds
            from .question_bridge import rounds_exhausted
            if rounds_exhausted(self, card, role):
                return dict(status='refused', reason='question_rounds_exhausted', exit_code=2)
            # Let the base service handle audited overrides, which never fall through.
            if seats or model is not None or leg is not None or not card:
                return await super()._spawn(
                    card=card, prompt=prompt, role=role, writer_families=writer_families,
                    review_of=review_of, model=model, leg=leg, override_reason=override_reason,
                    dry_run=dry_run, wait_seconds=wait_seconds, no_fallthrough=not seats or no_fallthrough,
                    benchmark=benchmark, require_bench=require_bench, review_scope=review_scope,
                    seats=seats,
                )
            from plangraph.settings import admit_review_scope
            admit_review_scope(card, role, review_scope)
            # A seat run (_plan from _spawn_seats) keeps to its own legs on re-route.
            allowed = {l.id for l in _plan.legs} if _plan is not None else None
            def restrict(p):
                return p if allowed is None else replace(p, legs=tuple(l for l in p.legs if l.id in allowed))
            plan = _plan if _plan is not None else self._route(card, role, writer_families, review_of, benchmark=benchmark, require_bench=require_bench)
            from .resolver import plan_for_launch_async
            if hasattr(plan, '__dataclass_fields__'):
                plan = await plan_for_launch_async(plan, dry_run=dry_run)
            run_id = 'run-' + uuid.uuid4().hex
            import hashlib
            record = dict(run_id=run_id, card_hash=hashlib.sha256(json.dumps(card, sort_keys=True).encode()).hexdigest(),
                role=role, plan=plan.to_dict(), chosen_leg=None, served_model=None,
                served_family=None, request_side_model=None, witness_kind=None,
                writer_family=None, status='running', started=time.time(),
                given_writer_families=list(writer_families) or ((source := self.result(review_of, attempts=SAVE_READ_ATTEMPTS)).get('given_writer_families') or [f for f in [source.get('writer_family')] if f] if review_of else []),
                ended=None, attempts=[], stdout_tail='', stderr_tail='', exit_code=None, cwd=getattr(self.runner, 'default_cwd', os.getcwd()))
            from .context_pack import annotate
            from .item_key import annotate_item
            pack = await asyncio.to_thread(annotate, record, prompt, card, record['cwd'])   # D-3622: bad context refuses; big-file warnings (D-3649: built once, off the loop)
            annotate_item(record, card)
            for skipped in plan.to_dict()['skipped']:
                if any(marker in str(skipped) for marker in ('not_served_on_host', 'harness_missing')):
                    record.setdefault('skipped', []).append(skipped)
                    record['attempts'].append(dict(leg=skipped.get('leg'), status='skipped',
                        skip_reason=skipped.get('skip_reason') or skipped.get('reason')))
            self._save(record)

            if not plan.legs or plan.error:
                host_skips = [s for s in plan.skipped if any(marker in str(s) for marker in ('not_served_on_host', 'harness_missing'))]
                exhausted = not plan.legs and (not plan.error or
                    bool(plan.skipped) and all(s['reason'] == 'limited' for s in plan.skipped))
                record.update(status='failed' if host_skips else 'limited' if exhausted else 'refused',
                              exit_code=1 if host_skips else 4 if exhausted else 2, ended=time.time(),
                              error='; '.join(s.get('skip_reason') or s.get('reason', '') for s in host_skips) if host_skips else plan.error,
                              evidence=plan.to_dict()['skipped'])
                self._save(record)
                return self.result(run_id, attempts=SAVE_READ_ATTEMPTS)

            async def execute():
                tried = set()
                current = plan
                try:
                    limits = thresholds(card, role=role)
                    live_log = str(self._path(run_id).parent / 'live.log')
                    record['live_log'] = live_log
                    for _ in range(len(plan.legs) + 2):
                        chosen = next((l for l in current.legs if l.id not in tried), None)
                        if chosen is None or current.error:
                            break
                        tried.add(chosen.id)
                        from .leg_probe import before_launch
                        probe_skip = await before_launch(self.runner, chosen, role, dry_run)
                        if probe_skip:
                            record['attempts'].append(dict(leg=asdict(chosen), status='skipped', skip_reason=probe_skip))
                            self._save(record)
                            if no_fallthrough:
                                break
                            continue
                        from .inline_diff import InlineDiffError
                        try:
                            leg_prompt = self._leg_prompt(prompt, card, role, chosen, pack, document_review=leg is not None or dry_run)
                        except InlineDiffError as exc:
                            record['attempts'].append(dict(leg=asdict(chosen), status='skipped',
                                skip_reason='inline_diff_failed: ' + redact(str(exc))))
                            continue
                        leg_prompt, writer_env = writer_launch(leg_prompt, role, chosen.model, run_id)
                        job = await self.runner.spawn(prompt=leg_prompt, model_or_combo=chosen.model,
                            role=role, dry_run=dry_run, client=chosen.harness, watchdog=limits, live_log=live_log, extra_env=writer_env,
                            prompt_record=(self._path(run_id).with_name('prompt.txt'), record, self._save))  # REMOVE-AT-R10
                        self._running_records[job.job_id] = record
                        process = self.runner._processes.get(job.job_id)
                        record.update(job_id=job.job_id, pid=process.pid if process else None,
                                      **{k: bool(v) if k == 'job_object' else v for k, v in getattr(self.runner, '_owned', {}).get(job.job_id, {}).items() if k != 'pid'},
                                      writer_family=chosen.family,
                                      cwd=job.cwd or getattr(self.runner, 'default_cwd', os.getcwd()))
                        self._save(record)
                        task = self.runner._tasks.get(job.job_id)
                        if task:
                            await asyncio.wait([task])   # a monitor cancelled by runner.cancel ends the leg, not this loop
                            if task.cancelled():
                                from .cancel_job import publish_cancel; publish_cancel(job)
                            elif not task.cancelled() and task.exception() is not None:
                                raise task.exception()
                        if job.survivors:   # kill_incomplete: terminal, no fall-through leg, no second writer
                            record['attempts'].append(dict(leg=asdict(chosen), status='failed', skip_reason='kill_incomplete'))
                            record.update(status='failed', exit_code=1, reason='kill_incomplete', survivors=job.survivors,
                                events=record.get('events', []) + job.events)
                            return
                        if self._path(run_id).with_name('cancelled').exists():   # D-3505: a CLI cancel from
                            job.status = 'cancelled'   # another process ends the run, never a fall-through leg
                        stop = self.runner.provider_responses.get(job.job_id)
                        if job.status in ('stalled', 'timeout', 'cancelled'):   # Q14, Q15/M16: terminal, with its attempt row
                            record['attempts'].append(dict(leg=asdict(chosen), status=job.status, skip_reason=job.status,
                                served_model=job.served_model or 'unwitnessed', served_family=job.served_family or 'unwitnessed'))
                            record.update(status=terminal_status(job.status, role), exit_code=1 if job.exit_code is None else job.exit_code, reason=job.status,
                                watchdog=next((e for e in job.events if e['event'].startswith('watchdog_')), None),
                                events=record.get('events', []) + job.events)   # D-3649: keep annotate()'s events
                            return
                        refusal = next((e for e in job.events if e.get('event') == 'model_refusal'), None)
                        if refusal is not None:   # terminal safety stop: no booking, reroute, or writer continuation
                            record['attempts'].append(dict(leg=asdict(chosen), status='failed', skip_reason='model_refusal',
                                stdout_tail=job.stdout, stderr_tail=job.stderr))
                            record.update(status='failed', reason='model_refusal', exit_code=job.exit_code,
                                stop_reason=refusal['stop_reason'], refusal_text_chars=refusal['refusal_text_chars'],
                                events=record.get('events', []) + job.events)
                            return
                        reason = None
                        unavailable = fallthrough_reason(self, chosen, job, record)
                        if unavailable:
                            job.status = 'failed'
                            job.exit_code = job.exit_code or 1
                        if job.status in ('failed', 'skipped'):
                            reason = redact(failure_reason(job, stop))
                        if unavailable:
                            reason = redact(unavailable)
                        record['attempts'].append(dict(leg=asdict(chosen), status=job.status,
                            served_model=job.served_model, served_family=job.served_family,
                            served_witness_reason=job.served_witness_reason,
                            request_side_model=job.request_side_model, witness_kind=job.witness_kind,
                            skip_reason=reason or ('provider_stop' if stop else None), provider_response=stop,
                            stdout_tail=job.stdout, stderr_tail=job.stderr))
                        if stop and not unavailable and job.status != 'cancelled':
                            notify_leg_refused(self, record, chosen, reason or 'provider_stop')
                        if job.status in ('completed', 'dry_run') and not unavailable and not stop:
                            record['chosen_leg'] = asdict(chosen)
                        record.update(status=job.status, exit_code=job.exit_code if job.exit_code is not None else (0 if job.status == 'dry_run' else 1), served_model=job.served_model,
                            served_family=job.served_family, served_witness_reason=job.served_witness_reason,
                            request_side_model=job.request_side_model, witness_kind=job.witness_kind,
                            writer_family=chosen.family if job.served_family in (None, '', 'unwitnessed') else job.served_family,
                            stdout_tail=record.get('stdout_tail', '') + job.stdout,
                            stderr_tail=record.get('stderr_tail', '') + job.stderr,
                            events=record.get('events', []) + getattr(job, 'events', []))
                        if job.status == 'needs_input' or (not stop and not unavailable) or no_fallthrough or dry_run:
                            if not dry_run and job.status != 'needs_input':
                                await self._continue_writer(record, job.stdout)
                            return
                        current = plan if unavailable else self._route(card, role, writer_families, review_of, benchmark=benchmark, require_bench=require_bench)
                        if hasattr(current, '__dataclass_fields__'):
                            current = restrict(await plan_for_launch_async(current, dry_run=dry_run))
                        for skipped in current.to_dict()['skipped']:
                            if any(marker in str(skipped) for marker in ('not_served_on_host', 'harness_missing')) and skipped not in record.setdefault('skipped', []):
                                record['skipped'].append(skipped)
                                record['attempts'].append(dict(leg=skipped.get('leg'), status='skipped',
                                    skip_reason=skipped.get('skip_reason') or skipped.get('reason')))
                        record['plan'] = current.to_dict()
                    else:
                        record.update(status='failed', exit_code=1, error='attempt iteration limit exceeded')
                        return
                    if not any(a.get('status') != 'skipped' for a in record['attempts']):
                        record.update(status='failed', exit_code=1, error='; '.join(
                            a['skip_reason'] for a in record['attempts'] if a.get('skip_reason')) or 'no leg launched')
                    elif any(a.get('skip_reason') and any(m in a['skip_reason'].lower() for m in ('model unavailable', 'not served', 'unknown model', 'not_served_on_host', 'harness_missing')) for a in record['attempts']):
                        record.update(status='failed', exit_code=1,
                            error='; '.join(a['skip_reason'] for a in record['attempts'] if a.get('skip_reason')))
                    elif record['attempts'] and all(l.id in tried for l in current.legs) and current.legs:
                        record.update(status='failed', exit_code=1,
                            error='router repeated exhausted legs')
                    else:
                        record.update(status='limited', exit_code=4, evidence=current.to_dict()['skipped'])
                except Exception as exc:
                    from plangraph.planner.routing import BenchRequired, BenchmarkError, SpawnRefused
                    if isinstance(exc, SpawnRefused):
                        record.update(status='refused', exit_code=3 if isinstance(exc, BenchmarkError) else 2,
                                      refusal=type(exc).__name__, error=redact(str(exc)),
                                      evidence=list(exc.gaps) if isinstance(exc, BenchRequired) else [],
                                      legs=list(exc.legs) if isinstance(exc, BenchRequired) else [])
                        return
                    record.update(status='failed', exit_code=1, error=redact(str(exc)))
                finally:
                    if record['status'] == 'running' and record.get('job_id') in self.runner.jobs and self.runner.jobs[record['job_id']].survivors:
                        record.update(status='failed', exit_code=1, reason='kill_incomplete', survivors=self.runner.jobs[record['job_id']].survivors)
                    elif record['status'] == 'running':
                        record.update(status='cancelled', exit_code=1)
                    record['ended'] = time.time()
                    terminal_reason(record, job if record.get('job_id') else None)
                    release_attempts(self, record)
                    self._save(record)

            task = asyncio.create_task(execute())
            self.tasks[run_id] = task
            if dry_run or wait_seconds is None:
                await task
            elif wait_seconds > 0:
                try:
                    await asyncio.wait_for(asyncio.shield(task), wait_seconds)
                except asyncio.TimeoutError:
                    pass
            return self.result(run_id, attempts=SAVE_READ_ATTEMPTS)

    return DispatchService(*args, **kwargs)
