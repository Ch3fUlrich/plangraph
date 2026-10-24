"""Async job pool for executing and managing spawned agents via OmniRoute ACP.

Features:
  - Async SpawnerRunner managing non-blocking agent executions
  - Tracks active jobs (spawn, status, steer, cancel)
  - Extracts served_model and served_family from response headers or output
  - OmniRoute ACP-first execution with full Windows Tailnet environment preservation
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Sequence

from plangraph.spawner.clients import (
    build_child_env,
    build_command,
    build_routing_envelope_headers,
)
from plangraph.spawner.resolver import detect_model_family
from plangraph.spawner.mimo_policy import check_mimo_path

_SELECTED_MODEL_RE = re.compile(r"(?i)X-OmniRoute-Selected-Model:\s*([^\r\n]+)")
_SELECTED_FAMILY_RE = re.compile(r"(?i)X-OmniRoute-Selected-Family:\s*([^\r\n]+)")


def redact(value):
    """Scrub credential-shaped strings recursively before persistence or display."""
    if isinstance(value, dict):
        return {k: ('[REDACTED]' if re.search(r'(?i)(key|authorization|token|secret)', k)
                    else redact(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        value = re.sub(r'sk-[\w-]+', '[REDACTED]', value)
        value = re.sub(r'(?i)Bearer\s+[^\s"\x27]+', 'Bearer [REDACTED]', value)
        return re.sub(r'(?i)(\b\w*_KEY=)[^\s"\x27]+', r'\1[REDACTED]', value)
    return value


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
    served_model: str | None = None
    served_family: str | None = None
    dry_run: bool = False
    events: list[dict] = field(default_factory=list)
    steer_messages: list[str] = field(default_factory=list)

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
        model_or_combo: str = "l2-worker",
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
        check_mimo_path(model_or_combo)
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

        if dry_run:
            served_model = model_or_combo
            served_family = detect_model_family(model_or_combo)
            if "/" not in model_or_combo:
                from plangraph.spawner.resolver import get_catalog_models
                combo_legs = [
                    m for m in get_catalog_models()
                    if model_or_combo in (m.get("combos") or []) and int(m.get("available", 1)) == 1
                ]
                if combo_legs:
                    served_model = combo_legs[0]["id"]
                    served_family = detect_model_family(served_model)
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
                stdout=f"[dry-run] Planned command: {' '.join(cmd)}",
                served_model=served_model,
                served_family=served_family,
                dry_run=True,
            )
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
        )
        self.jobs[job_id] = job

        import shutil
        resolved_exe = shutil.which(cmd[0], path=child_env.get("PATH")) or cmd[0]
        self._adapter_tokens[job_id] = object()
        try:
            proc = await self.process_factory(
                resolved_exe,
                *cmd[1:],
                cwd=work_dir,
                env=child_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
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

    async def _monitor_process(
        self,
        job_id: str,
        proc: asyncio.subprocess.Process,
        requested_model: str,
    ) -> None:
        """Monitor process execution until completion."""
        try:
            stdout_bytes, stderr_bytes = await proc.communicate()
            stdout_text = stdout_bytes.decode("utf-8", errors="replace")
            stderr_text = stderr_bytes.decode("utf-8", errors="replace")
            exit_code = proc.returncode

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
            job.stdout = redact(stdout_text)
            job.stderr = redact(stderr_text)
            job.exit_code = exit_code
            job.end_time = time.time()
            job.status = "completed" if exit_code == 0 else "failed"

            served_model, served_family = extract_served_telemetry(
                stdout_text + "\n" + stderr_text,
                fallback_model=requested_model,
            )
            job.served_model = served_model
            job.served_family = served_family
        except asyncio.CancelledError:
            if job_id in self.jobs:
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
            self._adapter_tokens.pop(job_id, None)
            self._nonces.pop(job_id, None)
            self._processes.pop(job_id, None)

    async def status(self, job_id: str | None = None) -> Any:
        """Query status of a specific job or all jobs."""
        if job_id:
            if job_id not in self.jobs:
                raise KeyError(f"Job {job_id} not found")
            return self.jobs[job_id]
        return [job for job in self.jobs.values()]

    async def steer(self, job_id: str, message: str) -> dict[str, Any]:
        """Send steer instruction to an active job."""
        if job_id not in self.jobs:
            raise KeyError(f"Job {job_id} not found")
        job = self.jobs[job_id]
        job.steer_messages.append(message)

        proc = self._processes.get(job_id)
        if not proc or getattr(proc, 'stdin', None) is None:
            return {'ok': False, 'steered': False, 'error': 'no stdin'}
        if proc.stdin.is_closing():
            return {'ok': False, 'steered': False, 'error': 'stdin closing'}
        if not proc.stdin.is_closing():
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
        return {"job_id": job_id, "status": "cancelled"}


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
        def route(self, card, role='implement', writer_families=(), review_of=None, **options):
            from . import leg_state
            reader = getattr(self.leg_reader, 'leg_status', self.leg_reader) or leg_state.leg_status
            original = self.leg_reader
            self.leg_reader = lambda model, **kw: reader(model if '/' in model else 'agy/' + model, **kw)
            try:
                return super().route(card, role, writer_families, review_of, **options)
            finally:
                self.leg_reader = original

        async def _spawn(self, card=None, prompt='', role='implement', writer_families=(),
                        review_of=None, model=None, leg=None, override_reason=None,
                        dry_run=False, wait_seconds=0, no_fallthrough=False, benchmark=None, require_bench=False):
            # Let the base service handle audited overrides, which never fall through.
            if model or leg or not card:
                return await super()._spawn(card, prompt, role, writer_families, review_of,
                    model, leg, override_reason, dry_run, wait_seconds, True, benchmark, require_bench)
            plan = self.route(card, role, writer_families, review_of, benchmark=benchmark, require_bench=require_bench)
            run_id = 'run-' + uuid.uuid4().hex
            import hashlib
            record = dict(run_id=run_id, card_hash=hashlib.sha256(json.dumps(card, sort_keys=True).encode()).hexdigest(),
                role=role, plan=plan.to_dict(), chosen_leg=None, served_model=None,
                served_family=None, writer_family=None, status='running', started=time.time(),
                ended=None, attempts=[], stdout_tail='', exit_code=None)
            self._save(record)

            if not plan.legs or plan.error:
                record.update(status='limited', exit_code=3, ended=time.time(),
                              evidence=plan.to_dict()['skipped'])
                self._save(record)
                return self.result(run_id)

            async def execute():
                tried = set()
                current = plan
                try:
                    for _ in range(len(plan.legs)):
                        chosen = next((l for l in current.legs if l.id not in tried), None)
                        if chosen is None or current.error:
                            break
                        tried.add(chosen.id)
                        job = await self.runner.spawn(prompt=prompt, model_or_combo=chosen.model,
                            role=role, dry_run=dry_run, client=chosen.harness)
                        process = self.runner._processes.get(job.job_id)
                        record.update(job_id=job.job_id, pid=process.pid if process else None,
                                      chosen_leg=asdict(chosen), writer_family=chosen.family)
                        self._save(record)
                        task = self.runner._tasks.get(job.job_id)
                        if task:
                            await asyncio.shield(task)
                        stop = self.runner.provider_responses.get(job.job_id)
                        record['attempts'].append(dict(leg=asdict(chosen), status=job.status,
                            skip_reason='provider_stop' if stop else None, provider_response=stop))
                        record.update(status=job.status, exit_code=job.exit_code if job.exit_code is not None else (0 if job.status == 'dry_run' else 1), served_model=job.served_model,
                            served_family=job.served_family, writer_family=job.served_family or chosen.family,
                            stdout_tail=redact(job.stdout)[-16000:], events=record.get('events', []) + getattr(job, 'events', []))
                        if not stop or no_fallthrough or dry_run:
                            return
                        current = self.route(card, role, writer_families, review_of, benchmark=benchmark, require_bench=require_bench)
                        record['plan'] = current.to_dict()
                    record.update(status='limited', exit_code=3, evidence=current.to_dict()['skipped'])
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
            return self.result(run_id)

    return DispatchService(*args, **kwargs)
