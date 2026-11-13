"""Process monitoring extracted from the job runner."""
from __future__ import annotations
from . import runner

def _nt(self) -> bool:
    return self.platform == 'nt' or runner._is_nt()

async def kill_recorded(self, pid, pid_start=None, job_name=None, escapees=()):
    """Cancel after a service restart: prove the recorded root by its persisted start token,
        kill its tree, verify.  Returns survivors ([] = clean); unprovable and alive = survivors."""
    plat = 'nt' if self._nt() else None
    if plat == 'nt' and type(pid_start) is int:
        from .win_recorded import kill
        return await kill(pid, pid_start, job_name, escapees)
    if pid_start is None or plat == 'nt':
        return [pid] if plat == 'nt' else runner.unproven_survivors(pid, plat)
    return await runner.kill_proven(self, pid, pid_start, plat=plat) or []

async def _own_kill(self, pid, ident=None):
    """Default killer: posix signals only a group proven ours by (pid, starttime); nt kills via the Job Object."""
    if self._nt():
        return
    if ident is None:
        st = runner.proc_kill._stat(pid)
        ident = next((o['pid_start'] for o in getattr(self, '_owned', {}).values() if o.get('pid') == pid and st and (o.get('pid_start') == st[4])), None)
    await runner.kill_process_tree(pid, ident=ident)

async def _reap_process(self, job_id: str, proc: runner.Any) -> None:
    reaped = getattr(self, '_reaped_jobs', None)
    if reaped is None:
        self._reaped_jobs = set()
        reaped = self._reaped_jobs
    if job_id in reaped:
        return
    reaped.add(job_id)
    await runner.reap(self, job_id, proc)

async def _monitor_process(self, job_id: str, proc: runner.asyncio.subprocess.Process, requested_model: str) -> None:
    """Monitor process execution until completion."""
    try:
        from .run_watchdog import collect
        stdout_bytes, stderr_bytes = await collect(self, job_id, proc)
        stdout_text = stdout_bytes.decode('utf-8', errors='replace')
        stderr_text = stderr_bytes.decode('utf-8', errors='replace')
        exit_code = self.jobs[job_id].exit_code if self.jobs[job_id].status in ('stalled', 'timeout', 'cancelled') or self.jobs[job_id].survivors or getattr(self.jobs[job_id], 'refusal', None) else proc.returncode
        job = self.jobs[job_id]
        for line in stderr_text.splitlines():
            try:
                envelope = runner.json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(envelope, dict) or set(envelope) != {'autoos_provider_response'}:
                from .proc_kill import record_oom_envelope
                record_oom_envelope(job.events, envelope)
                continue
            response = envelope['autoos_provider_response']
            if not isinstance(response, dict) or response.get('nonce') != self._nonces.get(job_id) or (not self._nonces.get(job_id)):
                job.events.append({'event': 'forged_envelope'})
                continue
            response = {k: v for k, v in response.items() if k != 'nonce'}
            if isinstance(response, dict) and set(response) == {'leg', 'status', 'body', 'headers'} and isinstance(response['leg'], str) and response['leg'] and (type(response['status']) is int) and (response['status'] in (429, 402)) and isinstance(response['body'], str) and response['body'].strip() and isinstance(response['headers'], dict) and all((isinstance(k, str) and isinstance(v, str) for k, v in response['headers'].items())):
                self.provider_response(job_id, **response, source='stderr_envelope', nonce=self._nonces[job_id])
        from .env_redact import redact_env_values
        env = self._child_envs.get(job_id, runner.os.environ)
        from .opencode_answer import answer_text, final_answer, terminated_after_answer, native_answer, record_steps
        record_steps(job, stdout_text, stderr_text, env)
        decoded, stderr_text, exit_code = native_answer(job, answer_text(stdout_text) if runner.is_opencode(job) else stdout_text, stderr_text, exit_code)
        job.stdout = runner.redact_output(redact_env_values(decoded, env))
        job.stderr = runner.redact_output(redact_env_values(stderr_text, env))
        job.exit_code = exit_code
        job.end_time = runner.time.time()
        if job.status not in ('stalled', 'timeout', 'cancelled'):
            tails = [text.rstrip('\r\n').splitlines() for text in (stdout_text, final_answer(stdout_text, allow_unfinished=True) or '')]
            review_verdict = exit_code == 1 and runner.is_opencode(job) and (job.role == 'review') and any((event.get('event') == 'opencode_tools_denied' for event in job.events)) and any((lines and lines[-1] in ('VERDICT: ACCEPT', 'VERDICT: REQUEST_CHANGES') for lines in tails))
            job.status = 'completed' if exit_code == 0 or review_verdict else 'failed'
            if not review_verdict and terminated_after_answer(job, stdout_text, stderr_text, exit_code):
                job.status = 'terminated_after_answer'
                job.events.append({'event': 'terminated_after_answer'})
        if job.status == 'failed' and runner.is_opencode(job):
            job.events.append(dict(event='harness_failed', error=runner.failure_reason(job)))
        from .opencode_isolation import fail_unchanged_writer
        from .question_bridge import question_exit
        if not question_exit(job, {}):
            await runner.asyncio.to_thread(fail_unchanged_writer, job, job.cwd)
        if job.command and job.command[0] == 'opencode' and (job.role == 'review'):
            oc_dir = self._ocdata_dirs.get(job_id) if exit_code == 0 else None
            denied = runner._opencode_tools_denied(env.get('OPENCODE_CONFIG_CONTENT') or '', env)
            m = await runner.asyncio.to_thread(runner._opencode_served_model, runner.Path(oc_dir) / 'opencode' / 'opencode.db', f'plangraph-{job_id}') if oc_dir and denied else None
            if m is not None and m == requested_model:
                job.request_side_model = m
                job.witness_kind = 'request-side-opencode-store'
            elif oc_dir and denied:
                job.events.append({'event': 'opencode_witness_unavailable'})
            if denied:
                from .gateway_witness import witness
                await runner.asyncio.to_thread(witness, self, job_id, job, env)
        if job.command and job.command[0] == 'claude' and (runner.last_stop_reason(stdout_text) == 'refusal'):
            answer = runner.final_text(stdout_text)
            job.events.append(dict(event='model_refusal', stop_reason='refusal', refusal_text_chars=len(answer[0]) if answer else 0))
            job.status = 'failed'
        if job.command and job.command[0] == 'claude' and (exit_code == 0):
            m = runner._claude_stream_model(stdout_text)
            if m is not None and m == requested_model:
                job.request_side_model = m
                job.witness_kind = 'request-side-claude-stream'
            await runner.asyncio.to_thread(runner.render_review_output, job, stdout_text, lambda text: runner.redact_output(redact_env_values(text, env)))
        await runner.witness_native(self, job_id, job, stdout_text, requested_model)
        job.served_model = job.served_model or 'unwitnessed'
        job.served_family = job.served_family or 'unwitnessed'
    except runner.asyncio.CancelledError:
        await self._reap_process(job_id, proc)
        if job_id in self.jobs:
            from .cancel_job import publish_cancel
            publish_cancel(self.jobs[job_id])
            self.jobs[job_id].end_time = runner.time.time()
    except Exception as exc:
        job = self.jobs[job_id]
        job.status = 'failed'
        job.exit_code = 1
        job.end_time = runner.time.time()
        job.events.append({'event': 'monitor_failed', 'error': type(exc).__name__})
        runner.logging.getLogger(__name__).exception('monitor_failed: %s', type(exc).__name__)
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
