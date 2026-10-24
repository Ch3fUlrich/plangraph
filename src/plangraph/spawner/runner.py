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
                    except Exception:
                        pass

    if not served_model and fallback_model:
        served_model = fallback_model
    if not served_family and served_model:
        served_family = detect_model_family(served_model)

    return served_model, served_family


class SpawnerRunner:
    """Async process runner for Plangraph agent tasks."""

    def __init__(self, default_cwd: str | None = None):
        self.default_cwd = default_cwd or os.getcwd()
        self.jobs: dict[str, JobInfo] = {}
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._tasks: dict[str, asyncio.Task[Any]] = {}

    def provider_response(self, job_id, leg, status, body, headers, *, account=None, combo=None, store=None):
        """Gateway transport callback, NOT child stdout or agent-generated JSON.

        A gateway adapter must call this for the actual HTTP response, including
        a provider stop before the child exits. Exit codes alone are not evidence.
        """
        from plangraph.spawner import leg_state

        if job_id not in self.jobs:
            raise KeyError(job_id)
        target = store or leg_state
        if status in (429, 402):
            target.record_provider_response(leg, status, body, headers, "runner:gateway-response", account=account, combo=combo)
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
    ) -> JobInfo:
        """Spawn a new agent job and track its state."""
        check_mimo_path(model_or_combo)
        job_id = f"job-{uuid.uuid4().hex[:8]}"
        work_dir = cwd or self.default_cwd

        # Build routing envelope headers if not provided
        envelope_headers = dict(headers or {})
        if not envelope_headers:
            envelope_headers = build_routing_envelope_headers(
                role=role,
                risk=risk,
                min_intelligence=44.0,
                required_context=32000,
            )

        cmd = build_command(
            prompt=prompt,
            model_or_combo=model_or_combo,
            role=role,
            risk=risk,
            gateway=gateway,
            gateway_online=gateway_online,
            headers=envelope_headers,
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
        proc = await asyncio.create_subprocess_exec(
            resolved_exe,
            *cmd[1:],
            cwd=work_dir,
            env=child_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
        )
        self._processes[job_id] = proc

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
            job.stdout = stdout_text
            job.stderr = stderr_text
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
        finally:
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
        if proc and proc.stdin and not proc.stdin.is_closing():
            try:
                proc.stdin.write((message + "\n").encode("utf-8"))
                await proc.stdin.drain()
            except Exception:
                pass

        return {"job_id": job_id, "steered": True, "message": message, "status": job.status}

    async def cancel(self, job_id: str) -> dict[str, Any]:
        """Cancel a running job."""
        if job_id not in self.jobs:
            raise KeyError(f"Job {job_id} not found")
        job = self.jobs[job_id]

        proc = self._processes.get(job_id)
        if proc:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass

        task = self._tasks.get(job_id)
        if task and not task.done():
            task.cancel()

        job.status = "cancelled"
        job.end_time = time.time()
        return {"job_id": job_id, "status": "cancelled"}
