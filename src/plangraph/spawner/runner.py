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
import sqlite3
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from plangraph.spawner.clients import (
    STDIN_PROMPT_HARNESSES,
    build_child_env,
    build_command,
    build_routing_envelope_headers,
)
from plangraph.spawner.resolver import MODEL_FAMILIES, detect_model_family
from plangraph.spawner.mimo_policy import check_mimo_path
from plangraph.planner.legs import DEFAULT_LEGS

_SELECTED_MODEL_RE = re.compile(r"(?i)X-OmniRoute-Selected-Model:\s*([^\r\n]+)")
_SELECTED_FAMILY_RE = re.compile(r"(?i)X-OmniRoute-Selected-Family:\s*([^\r\n]+)")

_KEEPER_UNCONFIGURED_WARN_S = 600
_keeper_unconfigured_last_warned = None


from .env_redact import unavailable_reason


def redact(value):
    """Scrub credential-shaped strings recursively before persistence or display."""
    if isinstance(value, dict):
        return {k: ('[REDACTED]' if k != 'idempotency_key' and re.search(r'(?i)(key|authorization|token|secret)', k)
                    else redact(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        value = re.sub(r'sk-[\w-]+', '[REDACTED]', value)
        value = re.sub(r'(?i)Bearer\s+[^\s"\x27]+', 'Bearer [REDACTED]', value)
        return re.sub(r'(?i)(\b\w*_KEY=)[^\s"\x27]+', r'\1[REDACTED]', value)
    return value


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


@dataclass
class JobInfo:
    """Execution state of a spawned agent task."""
    job_id: str
    status: str  # "running" | "completed" | "failed" | "cancelled" | "dry_run"
    task_prompt: str
    model_or_combo: str
    role: str = "implement"
    risk: str = "normal"
    command: list[str] = field(default_factory=list)
    start_time: float = field(default_factory=time.time)
    end_time: float | None = None
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    served_model: str | None = 'unwitnessed'
    served_family: str | None = 'unwitnessed'
    served_witness_reason: str = 'harness supplied no authenticated served-model telemetry'
    dry_run: bool = False
    events: list[dict] = field(default_factory=list)
    steer_messages: list[str] = field(default_factory=list)
    cwd: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def extract_served_telemetry(output: str, fallback_model: str = "") -> tuple[str, str]:
    """Extract served model and family from OmniRoute headers or JSON output."""
    served_model = ""
    served_family = ""

    if output:
        # Check header patterns
        m_mod = _SELECTED_MODEL_RE.search(output)
        if m_mod:
            served_model = m_mod.group(1).strip()

        m_fam = _SELECTED_FAMILY_RE.search(output)
        if m_fam:
            served_family = m_fam.group(1).strip()

        # Check JSON payload if present
        if not served_model or not served_family:
            for line in output.splitlines():
                line = line.strip()
                if line.startswith("{") and line.endswith("}"):
                    try:
                        data = json.loads(line)
                        if isinstance(data, dict):
                            if not served_model and "served_model" in data:
                                served_model = str(data["served_model"])
                            if not served_family and "served_family" in data:
                                served_family = str(data["served_family"])
                    except (ValueError, TypeError):
                        continue
                    except Exception as exc:
                        logging.getLogger(__name__).warning('telemetry_failed: %s', type(exc).__name__)
                        continue

    if not served_family and served_model:
        served_family = detect_model_family(served_model)

    return served_model, served_family


def _opencode_served_model(db_path, title) -> str | None:
    """Served model witnessed in opencode's session store for one titled session (D-2898).

    Only a unanimous assistant model id counts; anything else stays unwitnessed.
    """
    try:
        conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
        try:
            rows = conn.execute('SELECT id FROM session_v2 WHERE title = ?', (title,)).fetchall()
            models = set()
            for (data,) in conn.execute(
                    "SELECT data FROM session_message WHERE session_id = ? AND type = 'assistant'",
                    (rows[0][0],) if len(rows) == 1 else (None,)):
                model = json.loads(data)['model']
                models.add(model['providerID'] + '/' + model['id'])
        finally:
            conn.close()
    except (sqlite3.Error, OSError, ValueError, KeyError, TypeError):
        return None
    return next(iter(models)) if len(models) == 1 else None


class NonceCapacity(RuntimeError):
    """All nonce slots belong to live runs."""


class SpawnerRunner:
    """Async process runner for Plangraph agent tasks."""

    def __init__(self, default_cwd: str | None = None, *, process_factory=None, tree_killer=None):
        self.default_cwd = default_cwd or os.getcwd()
        self.jobs: dict[str, JobInfo] = {}
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self.process_factory = process_factory or asyncio.create_subprocess_exec
        self.tree_killer = tree_killer or kill_process_tree
        self.provider_responses = {}
        self._nonces = {}
        self._adapter_tokens = {}
        self._child_envs = {}
        self._git_guards = {}
        self._stdin_payloads = {}

    def served_witness(self, job_id, model, *, adapter_token):
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
        extra_env: Mapping[str, str] | None = None,
        gateway: bool = True,
        gateway_online: bool = True,
        wait_seconds: float = 0.0,
        client: str | None = None,
    ) -> JobInfo:
        """Spawn a new agent job and track its state."""
        from plangraph.planner.routing import validate_model
        validate_model(model_or_combo)
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
        render_reason = None
        if cmd[0] == 'opencode':
            # Name the session so the monitor can witness the served model (D-2898).
            cmd.extend(['--title', f'plangraph-{job_id}'])
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
                self.jobs[job_id] = job
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
            )
            self.jobs[job_id] = job

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

    async def _monitor_process(
        self,
        job_id: str,
        proc: asyncio.subprocess.Process,
        requested_model: str,
    ) -> None:
        """Monitor process execution until completion."""
        try:
            payload = self._stdin_payloads.get(job_id)

            async def _comm(input_data):
                if input_data is not None:
                    try:
                        return await proc.communicate(input=input_data)
                    except TypeError:
                        pass
                return await proc.communicate()

            timeout = os.environ.get('PLANGRAPH_SPAWN_RUN_TIMEOUT')
            if timeout:
                try:
                    seconds = float(timeout)
                    if not math.isfinite(seconds) or seconds <= 0:
                        raise ValueError('invalid run timeout')
                    stdout_bytes, stderr_bytes = await asyncio.wait_for(_comm(payload), seconds)
                except (asyncio.TimeoutError, ValueError):
                    await self.tree_killer(proc.pid)
                    stdout_bytes, stderr_bytes = await proc.communicate()
                    self.jobs[job_id].events.append({'event': 'run_timeout'})
                    self.jobs[job_id].exit_code = 124
            else:
                stdout_bytes, stderr_bytes = await _comm(payload)
            stdout_text = stdout_bytes.decode("utf-8", errors="replace")
            stderr_text = stderr_bytes.decode("utf-8", errors="replace")
            exit_code = self.jobs[job_id].exit_code if self.jobs[job_id].exit_code == 124 else proc.returncode

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
            job.status = "completed" if exit_code == 0 else "failed"

            # Witness an opencode leg only from opencode's session store, only
            # for the requested model id (D-2898).
            if (job.command and job.command[0] == 'opencode' and exit_code == 0
                    and (not job.served_model or job.served_model == 'unwitnessed')):
                child_env = self._child_envs.get(job_id, os.environ)
                db_path = (Path(child_env.get('XDG_DATA_HOME') or Path.home() / '.local' / 'share')
                           / 'opencode' / 'opencode.db')
                m = _opencode_served_model(db_path, f'plangraph-{job_id}')
                if m is not None and m == requested_model:
                    try:
                        self.served_witness(job_id, m, adapter_token=self._adapter_tokens.get(job_id))
                        job.served_witness_reason = 'opencode session store'
                    except ValueError:
                        job.events.append({'event': 'opencode_witness_unavailable'})
                elif m is not None:
                    job.events.append({'event': 'opencode_witness_unavailable'})

            # Free text is child-controlled, including plausible provider headers.
            # Absence of an authenticated adapter witness must remain explicit.
            job.served_model = job.served_model or 'unwitnessed'
            job.served_family = job.served_family or 'unwitnessed'
        except asyncio.CancelledError:
            if job_id in self.jobs:
                if self.jobs[job_id].status != "failed":
                    self.jobs[job_id].status = "cancelled"
                self.jobs[job_id].end_time = time.time()
                self.jobs[job_id].exit_code = 1
        except Exception as exc:
            job = self.jobs[job_id]
            job.status = 'failed'
            job.exit_code = 1
            job.end_time = time.time()
            job.events.append({'event': 'monitor_failed', 'error': type(exc).__name__})
            logging.getLogger(__name__).exception('monitor_failed: %s', type(exc).__name__)
        finally:
            self._check_git_config_guard(job_id)
            self._git_guards.pop(job_id, None)
            self._stdin_payloads.pop(job_id, None)
            self._child_envs.pop(job_id, None)
            self._adapter_tokens.pop(job_id, None)
            self._nonces.pop(job_id, None)
            self._processes.pop(job_id, None)

    @staticmethod
    def _init_git_config_guard(work_dir: str | Path | None) -> tuple[Path, bytes, str] | None:
        if not work_dir:
            return None
        try:
            proc = subprocess.run(
                ["git", "-C", str(work_dir), "rev-parse", "--git-common-dir"],
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode != 0:
                return None
            raw_common = proc.stdout.strip()
            if not raw_common:
                return None
            common_dir = (Path(work_dir) / raw_common).resolve()
            config_path = common_dir / "config"
            if not config_path.is_file():
                return None
            orig_bytes = config_path.read_bytes()
            orig_hash = hashlib.sha256(orig_bytes).hexdigest()
            return (config_path, orig_bytes, orig_hash)
        except (OSError, UnicodeDecodeError):
            return None

    def _check_git_config_guard(self, job_id: str) -> None:
        guard = self._git_guards.get(job_id)
        if not guard:
            return
        config_path, orig_bytes, orig_hash = guard
        try:
            curr_bytes = config_path.read_bytes() if config_path.is_file() else None
        except OSError:
            curr_bytes = None
        curr_hash = hashlib.sha256(curr_bytes).hexdigest() if curr_bytes is not None else None
        if curr_hash != orig_hash:
            try:
                if orig_bytes is not None:
                    config_path.write_bytes(orig_bytes)
                else:
                    config_path.unlink(missing_ok=True)
            except OSError:
                pass
            if job_id in self.jobs:
                job = self.jobs[job_id]
                job.events.append({"event": "git_config_mutated"})
                job.status = "failed"
                if not job.exit_code or job.exit_code == 0:
                    job.exit_code = 1

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
        if not isinstance(job_id, str):
            from plangraph.routing_errors import ParameterRefused
            raise ParameterRefused('job_id must be a string')
        if job_id not in self.jobs:
            raise KeyError(f"Job {job_id} not found")
        job = self.jobs[job_id]

        proc = self._processes.get(job_id)
        if proc:
            try:
                await self.tree_killer(proc.pid)
            except ProcessLookupError:
                pass

        task = self._tasks.get(job_id)
        if task and not task.done():
            task.cancel()

        job.status = "cancelled"
        self._nonces.pop(job_id, None)
        job.end_time = time.time()
        self._check_git_config_guard(job_id)
        return {"job_id": job_id, "status": job.status}


async def kill_process_tree(pid, *, platform=None, command_factory=None):
    """Terminate the isolated POSIX group or Windows process tree."""
    if (platform or os.name) == 'nt':
        factory = command_factory or asyncio.create_subprocess_exec
        proc = await factory('taskkill', '/PID', str(pid), '/T', '/F',
                             stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        await proc.communicate()
    else:
        import signal
        os.killpg(pid, signal.SIGKILL)


def spawn_service(*args, **kwargs):
    """Shared hardened dispatch for the public CLI and MCP entry points."""
    from .service import SpawnService
    from dataclasses import asdict
    from pathlib import Path

    class DispatchService(SpawnService):
        def __init__(self, *args, completion_sink=None, keeper_sink=None, **kwargs):
            super().__init__(*args, **kwargs)
            from .notify import FileInbox
            self.completion_sink = completion_sink or FileInbox(self.state)
            from .notify import KeeperSink
            self.keeper_sink = keeper_sink or KeeperSink()

        def _save(self, record, *, retry=False):
            from .notify import completion_event, CommandSink, KeeperNotConfigured
            job = getattr(self.runner, 'jobs', {}).get(record.get('job_id'))
            if job is not None:
                for stream in ('stdout', 'stderr'):
                    record[stream + '_tail'] = getattr(job, stream, '')
                for attempt in record.get('attempts', []):
                    if attempt.get('status') in ('failed', 'skipped') and not attempt.get('skip_reason'):
                        attempt['skip_reason'] = (redact(job.stderr.strip().splitlines()[0])
                            if job.stderr.strip() else f'harness exit code {job.exit_code}')
            terminal = record.get('ended') is not None and record.get('status') != 'running'
            record.setdefault('wait_cmd', 'plangraph-spawn wait ' + record['run_id'])
            if not retry:
                super()._save(record)
            changed = False
            if terminal:
                if 'completion' not in record:
                    record['completion'] = completion_event(record, record.get('cwd') or getattr(self.runner, 'default_cwd', os.getcwd()))
                    changed = True
            if changed:
                super()._save(record)
            if terminal:
                for recipient, sink in (('parent', self.completion_sink), ('keeper', self.keeper_sink)):
                    marker = self._path(record['run_id']).parent / (recipient + '.delivered')
                    if marker.exists():
                        continue
                    try:
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
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError('run wait timed out: ' + run_id)
                await asyncio.sleep(.02)

        def _route(self, card, role='implement', writer_families=(), review_of=None, **options):
            from . import leg_state
            reader = getattr(self.leg_reader, 'leg_status', self.leg_reader) or leg_state.leg_status
            original = self.leg_reader
            self.leg_reader = lambda model, **kw: reader(model if '/' in model else 'agy/' + model, **kw)
            try:
                return super()._route(card, role, writer_families, review_of, **options)
            finally:
                self.leg_reader = original

        async def _spawn(self, card=None, prompt='', role='implement', writer_families=(),
                        review_of=None, model=None, leg=None, override_reason=None,
                        dry_run=False, wait_seconds=0, no_fallthrough=False, benchmark=None, require_bench=False, review_scope=None):
            from .private_io import SAVE_READ_ATTEMPTS
            # Let the base service handle audited overrides, which never fall through.
            if model is not None or leg is not None or not card:
                return await super()._spawn(
                    card=card, prompt=prompt, role=role, writer_families=writer_families,
                    review_of=review_of, model=model, leg=leg, override_reason=override_reason,
                    dry_run=dry_run, wait_seconds=wait_seconds, no_fallthrough=True,
                    benchmark=benchmark, require_bench=require_bench, review_scope=review_scope,
                )
            from plangraph.settings import admit_review_scope
            admit_review_scope(card, role, review_scope)
            plan = self._route(card, role, writer_families, review_of, benchmark=benchmark, require_bench=require_bench)
            from .resolver import plan_for_launch_async
            if hasattr(plan, '__dataclass_fields__'):
                plan = await plan_for_launch_async(plan, dry_run=dry_run)
            run_id = 'run-' + uuid.uuid4().hex
            import hashlib
            record = dict(run_id=run_id, card_hash=hashlib.sha256(json.dumps(card, sort_keys=True).encode()).hexdigest(),
                role=role, plan=plan.to_dict(), chosen_leg=None, served_model=None,
                served_family=None, writer_family=None, status='running', started=time.time(),
                ended=None, attempts=[], stdout_tail='', stderr_tail='', exit_code=None, cwd=getattr(self.runner, 'default_cwd', os.getcwd()))
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
                    for _ in range(len(plan.legs) + 2):
                        chosen = next((l for l in current.legs if l.id not in tried), None)
                        if chosen is None or current.error:
                            break
                        tried.add(chosen.id)
                        job = await self.runner.spawn(prompt=prompt, model_or_combo=chosen.model,
                            role=role, dry_run=dry_run, client=chosen.harness)
                        process = self.runner._processes.get(job.job_id)
                        record.update(job_id=job.job_id, pid=process.pid if process else None,
                                      writer_family=chosen.family,
                                      cwd=job.cwd or getattr(self.runner, 'default_cwd', os.getcwd()))
                        self._save(record)
                        task = self.runner._tasks.get(job.job_id)
                        if task:
                            await asyncio.shield(task)
                        stop = self.runner.provider_responses.get(job.job_id)
                        reason = None
                        unavailable = unavailable_reason(job.exit_code, job.stdout, job.stderr)
                        if unavailable:
                            job.status = 'failed'
                            job.exit_code = job.exit_code or 1
                        if job.status in ('failed', 'skipped'):
                            reason = redact(job.stderr.strip().splitlines()[0]) if job.stderr.strip() else ('provider_stop' if stop else f'harness exit code {job.exit_code}')
                        if unavailable:
                            reason = redact(unavailable)
                        record['attempts'].append(dict(leg=asdict(chosen), status=job.status,
                            served_model=job.served_model, served_family=job.served_family,
                            served_witness_reason=job.served_witness_reason,
                            skip_reason=reason or ('provider_stop' if stop else None), provider_response=stop,
                            stdout_tail=job.stdout, stderr_tail=job.stderr))
                        if job.status in ('completed', 'dry_run') and not unavailable and not stop:
                            record['chosen_leg'] = asdict(chosen)
                        record.update(status=job.status, exit_code=job.exit_code if job.exit_code is not None else (0 if job.status == 'dry_run' else 1), served_model=job.served_model,
                            served_family=job.served_family, served_witness_reason=job.served_witness_reason,
                            writer_family=chosen.family if job.served_family in (None, '', 'unwitnessed') else job.served_family,
                            stdout_tail=record.get('stdout_tail', '') + job.stdout,
                            stderr_tail=record.get('stderr_tail', '') + job.stderr,
                            events=record.get('events', []) + getattr(job, 'events', []))
                        if (not stop and not unavailable) or no_fallthrough or dry_run:
                            if not dry_run:
                                await self._continue_writer(record, job.stdout)
                            return
                        current = plan if unavailable else self._route(card, role, writer_families, review_of, benchmark=benchmark, require_bench=require_bench)
                        if hasattr(current, '__dataclass_fields__'):
                            current = await plan_for_launch_async(current, dry_run=dry_run)
                        for skipped in current.to_dict()['skipped']:
                            if any(marker in str(skipped) for marker in ('not_served_on_host', 'harness_missing')) and skipped not in record.setdefault('skipped', []):
                                record['skipped'].append(skipped)
                                record['attempts'].append(dict(leg=skipped.get('leg'), status='skipped',
                                    skip_reason=skipped.get('skip_reason') or skipped.get('reason')))
                        record['plan'] = current.to_dict()
                    else:
                        record.update(status='failed', exit_code=1, error='attempt iteration limit exceeded')
                        return
                    if any(a.get('skip_reason') and any(m in a['skip_reason'].lower() for m in ('model unavailable', 'not served', 'unknown model', 'not_served_on_host', 'harness_missing')) for a in record['attempts']):
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
                    if record['status'] == 'running':
                        record.update(status='cancelled', exit_code=1)
                    record['ended'] = time.time()
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
