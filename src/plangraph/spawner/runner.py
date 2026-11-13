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
from plangraph.planner.legs import DEFAULT_LEGS, EXPLICIT_LEGS
from plangraph.spawner.winjob import WinJobApi  # re-export: tests patch runner.WinJobApi
from plangraph.routing_support import background_process_options

_KEEPER_UNCONFIGURED_WARN_S = 600
# D-2966: writers commit in their clone; AutoOS leaf-agent profiles they may read must not fence that.
IMPLEMENT_HEADER = ("You run as the opencode build agent: git commit is allowed and required per your brief; "
                    "AutoOS leaf-agent profiles do not apply to you.")
_keeper_unconfigured_last_warned = None


from .env_redact import first_reason, redact, redact_output, _redact_prompt
from .exit_outcome import failure_reason, fallthrough_reason, is_opencode, notify_leg_refused, refusal_record, release_attempts, terminal_reason
from .git_guard import prepare_worktree, writer_launch
from .proc_kill import KillIncomplete, kill_process_tree, root_identity
from . import proc_kill
from .reap import kill_proven, reap, sample_tree, unproven_survivors
from .served_telemetry import _claude_stream_model, _opencode_served_model, extract_served_telemetry, mismatch_flag, witness_native  # re-export: tests import and patch these on runner


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
        from plangraph.routing_support.clients import WINDOWS_ARGV_LIMIT, WINDOWS_BATCH_ARGV_LIMIT
        limit = WINDOWS_BATCH_ARGV_LIMIT if is_batch else WINDOWS_ARGV_LIMIT
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
        return mismatch_flag(asdict(self))


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
        self.process_factory = process_factory or proc_kill.oom_process_factory
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
        client: str | None = None, reasoning_cap=False,
        watchdog=None, live_log=None, prompt_record=None, prepare_launch=False, wake_variant='initial',  # REMOVE-AT-R10
    ) -> JobInfo:
        """Spawn a new agent job and track its state."""
        from plangraph.planner.routing import validate_model
        from .target import resolve_target   # leg ids and admitted combos resolve before providers render
        model_or_combo, leg = resolve_target(model_or_combo)
        if leg is not None:
            if client is not None and client != leg.harness:
                from plangraph.routing_errors import SpawnRefused
                raise SpawnRefused('client disagrees with leg harness: ' + leg.id)
            client = leg.harness
        validate_model(model_or_combo)
        if role == "implement" and not prompt.startswith(IMPLEMENT_HEADER):
            prompt = IMPLEMENT_HEADER + "\n\n" + prompt
        # REMOVE-AT-R10: capture the final prompt before command construction.
        if prepare_launch and not dry_run:  # REMOVE-AT-R10
            raise ValueError('launch preparation requires dry run')
        prompt = _redact_prompt(prompt, redact)  # REMOVE-AT-R10
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
            # Completion framing distinguishes a final answer from partial text.
            if '--format' not in cmd:
                cmd[2:2] = ['--format', 'json']
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
            from .opencode_isolation import close_config_layers, review_credential_reason
            oc_dir = self._ocdata_dirs[job_id] = close_config_layers(child_env)
            if role == 'review':
                # D-2905 (1): the per-job dir is also the spawner-owned store, read only after child exit
                # -- the shared store is child-writable and proves nothing. REVIEW-WITNESS-OPENCODE R1:
                # no inherited config layer: HOME and XDG_CONFIG_HOME live in the per-job dir and every
                # caller-set OPENCODE_* override outside _REVIEW_OPENCODE_ENV_ALLOW is dropped.
                render_reason = review_credential_reason(model_or_combo, oc_dir, child_env, render_reason)
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
                from .opencode_answer import cap_reasoning
                if reasoning_cap and not cap_reasoning(child_env, model_or_combo):
                    render_reason = 'reasoning_output_limit: no supported reasoning cap'
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
                if "/" not in model_or_combo and not any(l.model == model_or_combo for l in (*DEFAULT_LEGS, *EXPLICIT_LEGS)):
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
                stdin_note = f" [stdin: {len(prompt.encode('utf-8'))} bytes]"
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
            job._watchdog_baseline = await asyncio.to_thread(edit_signature, work_dir) if role in ('implement', 'fix') else None
            if tools_event:
                job.events.append(tools_event)

            if render_reason:
                job.status, job.exit_code = 'failed', 1
                job.stderr, job.end_time = render_reason, time.time()
                job.events.append({'event': 'render_refused', 'reason': render_reason})
                return job

            from .resolver import host_skip_reason_async
            unavailable = await host_skip_reason_async(dict(model=model_or_combo, harness=cmd[0]), env=child_env)
            if unavailable:
                job.status, job.exit_code = 'failed', 1
                job.stderr, job.end_time = unavailable, time.time()
                return job
            from .resolver import admitted_by_render
            if admitted_by_render(model_or_combo):  # trace a later launch failure to D-2869
                job.events.append({'event': 'admitted_by_render', 'model': model_or_combo})

            import shutil
            resolved_exe = shutil.which(cmd[0], path=child_env.get("PATH")) or cmd[0]
            from plangraph.routing_support.clients import PromptTransportError, launch_stdin_payload
            try:
                payload = launch_stdin_payload(cmd, prompt, resolved_exe, is_nt=_is_nt())
            except PromptTransportError as exc:
                job.status, job.exit_code, job.stderr, job.end_time = 'failed', 1, str(exc), time.time()
                return job
            resolved_exe, launch_refusal = _resolve_launch(resolved_exe, cmd[1:], is_nt=_is_nt())
            if launch_refusal:
                job.status, job.exit_code = 'failed', 1
                job.stderr, job.end_time = launch_refusal, time.time()
                return job

            if payload is not None:
                self._stdin_payloads[job_id] = payload
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
                    stdin=asyncio.subprocess.PIPE if payload is not None else asyncio.subprocess.DEVNULL,
                    **background_process_options(new_session=True, platform='nt' if self._nt() else 'posix'),
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

    from .runner_monitor import _nt

    from .runner_monitor import kill_recorded

    from .runner_monitor import _own_kill

    from .runner_monitor import _reap_process

    from .runner_monitor import _monitor_process

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
    from .dispatch_service import spawn_service as create
    return create(*args, **kwargs)
