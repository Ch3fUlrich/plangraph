"""Per-run progress decisions, portable sampling, and incremental output capture."""
import asyncio
import codecs
import logging
import os
from pathlib import Path
import re
import subprocess
import time

from .run_cpu import cpu_seconds, proc_scan  # noqa: F401  re-exported for callers and tests
from .prompt_input import checkout_env
from .run_watchdog_limits import leg_limits, thresholds  # noqa: F401  re-exported for callers and tests
from .run_refusal import REFUSAL_MAX_FRAMES, REFUSAL_MAX_LINES, REFUSAL_WINDOW_S, RefusalGate  # noqa: F401  re-exported for callers and tests

ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)')
HEADER = re.compile(r'^>\s+\S+\s+·\s+\S+')
SKIP = {'.git', '.pytest_cache', '__pycache__', '.venv', 'logs', 'output', 'node_modules', 'site'}
LIVE_LOG_FLUSH = 16384        # batched appends: one write+fsync per 16 KB, not per line
LIVE_LOG_MAX = 2 * 1024 * 1024
LIVE_LOG_ERRORS = 3           # record at most this many live.log failures per run
READER_GRACE_S = 5.0          # bounded drain once the run is gone (M6)
EDIT_RETRY_S = 15.0           # retry cadence while the edit baseline is unreadable (Q7)


def terminal_status(status, role):
    return 'timed_out' if status == 'timeout' and role == 'review' else status


def watchdog_reason(job):
    if job.status != 'stalled':
        return job.status
    return next((e['rule'] for e in reversed(job.events)
                 if e.get('rule') in ('tool_loop', 'stuck') and e.get('event') == 'watchdog_stalled'), job.status)


def terminal_record(record, job):
    if job.status not in ('stalled', 'timeout'):
        return False
    status = terminal_status(job.status, record.get('role'))
    record.update(status=status, exit_code=job.exit_code, reason=watchdog_reason(job),
                  watchdog=next((e for e in job.events if e['event'].startswith('watchdog_')), None),
                  events=record.get('events', []) + job.events)   # D-3649: keep annotate()'s and earlier legs' events
    return True


def prepare_run(service, record, card):
    record['live_log'] = str(service._path(record['run_id']).parent / 'live.log')
    return thresholds(card, role=record.get('role')), record['live_log']


def is_activity(line):
    line = ANSI.sub('', line).strip()
    return bool(line and not HEADER.match(line))


class Watchdog:
    def __init__(self, limits, start, edit, writer=True):
        # Non-writer roles never edit and may buffer output: only 'max' applies.
        self.limits, self.start, self.edit, self.writer = limits, start, edit, writer
        self.edited, self.edit_baseline_known = False, edit is not None
        self.cpu = None
        self.cpu_idle_since = None
        self.progress = dict(edit=start, activity=start, cpu=start)

    def budget(self):
        """Hard deadline in seconds. A pre-edit writer whose edits we can see is owed its first-edit
        window on top of the table run budget (M5's 'holds while output flows' ends at max_s +
        first_edit_s); once edited, or when the edit signal is unavailable, or for non-writers,
        max_s ends the run. Hard caps (including PLANGRAPH_SPAWN_RUN_TIMEOUT and the default
        review cap) are wall clocks, never extended. The opt-in first-edit stall is separate."""
        if self.limits['max_s'] is None:
            return float('inf')
        if 'deadline' in self.limits:
            return max(0, min(self.limits['max_s'], self.limits['deadline'] - self.start))
        if self.limits.get('hard_max'):
            return self.limits['max_s']
        return self.limits['max_s'] + (self.limits['first_edit_s']
                                       if self.writer and not self.edited and self.edit is not None
                                       else 0)

    def decide(self, now, edit, activity, cpu, *, stdout_activity=None, waiting=False):
        if waiting: activity = now  # a visible live child CLI can be waiting on its provider
        if cpu is None:
            self.cpu_idle_since = None
        elif waiting or self.cpu is None or cpu != self.cpu:
            self.cpu_idle_since = now
        if edit is not None:
            if self.edit is None:
                # Q7/M4: the spawn baseline was unreadable; adopt the first
                # readable signature as baseline instead of pinning pre-edit.
                self.progress['edit'] = now
            elif edit != self.edit:
                self.edited = True
                self.progress['edit'] = now
            self.edit = edit
        if activity is not None:
            self.progress['activity'] = activity
        if self.edited and cpu is not None and self.cpu is not None and cpu > self.cpu:
            self.progress['cpu'] = now
        self.cpu = cpu
        stall = self.limits['stall_s']
        rule = None
        if now - self.start >= self.budget():
            rule = 'max'
        elif (self.cpu_idle_since is not None
              and now - max(self.cpu_idle_since, stdout_activity if stdout_activity is not None else activity or self.start)
              >= self.limits.get('stuck_s', stall + 300)):
            rule = 'stuck'
        elif not self.writer:
            pass
        elif not self.edited:
            if edit is None:
                # Q7/M4: edits are invisible this tick; no rule may assert
                # 'no worktree edit' (max is the backstop).
                pass
            elif self.limits.get('hard_first_edit') and self.edit_baseline_known and now - self.start >= self.limits['first_edit_s']:
                rule = 'no-first-edit'
            elif activity is not None and now - activity >= stall:
                # Q6: no activity signal (buffered harness) is not a stall.
                rule = 'no-activity'
            elif activity is None and now - self.start >= self.limits['first_edit_s']:
                # no-first-edit = no output AND no edit (M5/Q6, goal 1): 'no-activity'
                # above already owns a run that spoke. REMOVE-AT-R10 (D-3769).
                rule = 'no-first-edit'
        elif now - max(self.progress.values()) >= stall:
            rule = 'no-progress'
        if rule:
            return dict(event='watchdog_timeout' if rule == 'max' else 'watchdog_stalled',
                        **({'edit_evidence': 'changed' if self.edited else 'unchanged' if self.edit_baseline_known and edit is not None else 'unknown'} if self.limits.get('hard_first_edit') else {}),
                        rule=rule, phase='post-edit' if self.edited else 'pre-edit',
                        last_progress=max(self.progress.values()), last_activity_ts=activity,
                        quiet=[k for k, v in self.progress.items() if now - v >= stall
                               and not (k == 'cpu' and cpu is None)
                               and not (k == 'edit' and edit is None)],
                        unavailable=(['cpu'] if cpu is None else []) + (['edit'] if edit is None else []),
                        **{k: self.limits[k] for k in ('stall_s', 'first_edit_s', 'max_s')})


def edit_signature(cwd):
    try:
        count = size = newest = entries = 0
        for root, dirs, files in os.walk(cwd):
            dirs[:] = sorted(d for d in dirs if d not in SKIP)
            entries += len(dirs)
            if entries >= 100000:
                break
            for name in sorted(files):
                entries += 1
                if entries > 100000:
                    break
                try:
                    stat = os.stat(os.path.join(root, name), follow_symlinks=False)
                except OSError:
                    continue
                count += 1
                size += stat.st_size
                newest = max(newest, stat.st_mtime_ns)
            if entries > 100000:
                break
        head = subprocess.run(['git', '-C', str(cwd), 'rev-parse', 'HEAD'],
                              capture_output=True, timeout=10, check=False, env=checkout_env())
        return (count, newest, size), head.stdout if head.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


async def run_gone(proc):
    """Observe exit even when a detached grandchild holds pipes and Process.wait() open (M6)."""
    delay = .05
    while getattr(proc, 'returncode', None) is None:
        await asyncio.sleep(delay)
        delay = min(2.0, delay * 2)


class LiveLog:
    """Batched, error-tolerant live.log appender (Q4/M3/Q5)."""

    def __init__(self, job):
        self.job = job
        self.path = getattr(job, '_live_log', None)
        self.buf, self.pending, self.written, self.errors = [], 0, None, 0

    def feed(self, text):
        self.buf.append(text)
        self.pending += len(text)
        if self.pending >= LIVE_LOG_FLUSH:
            self.flush()

    def flush(self):
        text, self.buf, self.pending = ''.join(self.buf), [], 0
        if text:
            self.write(text)

    def write(self, text):
        from .private_io import append_private, atomic_write_private
        if not self.path or self.errors >= LIVE_LOG_ERRORS:
            return
        try:
            append_private(self.path, text)
            if self.written is None:
                try:
                    self.written = Path(self.path).stat().st_size
                except OSError:
                    self.written = len(text)
            else:
                self.written += len(text)
            if self.written > LIVE_LOG_MAX:
                data = Path(self.path).read_bytes()[-LIVE_LOG_MAX // 2:]
                atomic_write_private(self.path, data.decode('utf-8', 'replace'))
                self.written = len(data)
        except Exception as exc:
            # Q5: live.log trouble is an event, never a failed run.
            self.errors += 1
            try:
                self.job.events.append({'event': 'live_log_error', 'error': type(exc).__name__})
            except Exception:
                pass


async def collect(runner, job_id, proc):
    from .runner import redact
    from .reap import cancel_and_close
    from .env_redact import early_refusal, redact_env_values, OutputLines
    job = runner.jobs[job_id]
    env = runner._child_envs.get(job_id, os.environ)
    limits = getattr(job, '_watchdog_limits', None) or thresholds()
    baseline = getattr(job, '_watchdog_baseline', None)
    w = Watchdog(limits, job.start_time, baseline, writer=job.role in ('implement', 'fix'))
    captured = {'stdout': [], 'stderr': []}
    refused = asyncio.Event()
    seen = {'n': 0, 'r': 0}   # activity lines run-wide (stdout + stderr): the first REFUSAL_MAX_LINES lines and REFUSAL_MAX_FRAMES frames may speak a refusal
    def scrub(text):
        return redact(redact_env_values(text, env))
    gate = RefusalGate(refused, job, scrub)
    from .stall_probe import ToolLoop
    tools, loop_found = ToolLoop(), asyncio.Event()
    stdout_at = job.start_time
    async def reader(stream, name):
        pending, tail = '', ''
        log = LiveLog(job)
        output_lines = OutputLines(scrub)
        decoder = codecs.getincrementaldecoder('utf-8')('replace')
        def consume(text):
            nonlocal pending, tail, stdout_at
            if not text:
                return
            if name == 'stdout':
                stdout_at = time.time()
            captured[name].append(text)   # raw; kept even if a drain grace cancels us
            pending += text
            lines = pending.splitlines(keepends=True)
            pending = ''
            active = False
            candidates = []
            for line in lines:
                if not line.endswith(('\n', '\r')):
                    pending = line
                    continue
                if name == 'stdout' and tools.feed(line):
                    loop_found.set()
                if is_activity(line):
                    active = True
                    if seen['n'] < REFUSAL_MAX_LINES and seen['r'] < REFUSAL_MAX_FRAMES:   # F1: banner/spinner frames may precede the refusal
                        candidates.append(line)
                    seen['n' if line.endswith('\n') else 'r'] += 1   # F1: '\r'-alone spinner frames eat the frame cap, never the 40-line budget
            # F3: check the unterminated tail too; a refusal line printed
            # without a trailing newline while the child hangs must be seen.
            if pending and seen['n'] < REFUSAL_MAX_LINES and seen['r'] < REFUSAL_MAX_FRAMES:
                candidates.append(pending)   # tail is a candidate only, never counted
            found = candidates and not refused.is_set() and time.time() - job.start_time < REFUSAL_WINDOW_S and early_refusal(''.join(candidates))
            if found or active:   # F2: a refusal is booked only after a quiet period; later activity cancels it
                (gate.candidate(found, time.time()) if found else gate.activity(time.time()))
            gate.scan_late(lines, time.time())   # REMOVE-AT-R10 (D-3769)
            if active:
                job.last_activity_ts = time.time()
            scrubbed = output_lines.feed(text)
            tail = (tail + scrubbed)[-4096:]
            setattr(job, name, tail)
            log.feed(scrubbed)
            log.flush()  # live activity must not wait for a 16 KB batch or EOF
        try:
            while True:
                data = await stream.read(65536)
                if not data:
                    break
                consume(decoder.decode(data))
            consume(decoder.decode(b'', final=True))
            if pending and is_activity(pending):
                job.last_activity_ts = time.time()
        finally:
            remainder = output_lines.feed('', final=True)
            setattr(job, name, (tail + remainder)[-4096:])
            log.feed(remainder)
            log.flush()   # also on drain-grace cancellation: batched bytes land
    async def output():
        payload = runner._stdin_payloads.get(job_id)
        if all(isinstance(getattr(proc, n, None), asyncio.StreamReader) for n in ('stdout', 'stderr')):
            readers = [asyncio.create_task(reader(getattr(proc, n), n)) for n in ('stdout', 'stderr')]
            try:
                if payload is not None:
                    try:
                        if proc.stdin is None:
                            raise BrokenPipeError('stdin unavailable')
                        proc.stdin.write(payload)
                        await proc.stdin.drain()
                    except (BrokenPipeError, ConnectionResetError):
                        job.status, job.exit_code = 'failed', 1
                        job.stderr = 'stdin_delivery_failed: pipe unavailable or closed'
                        job.events.append({'event': 'stdin_delivery_failed'})
                        raise RuntimeError(job.stderr) from None
                    finally:
                        if proc.stdin is not None:
                            proc.stdin.close()
                drain = asyncio.ensure_future(asyncio.gather(*readers, return_exceptions=True))
                gone = asyncio.ensure_future(run_gone(proc))
                await asyncio.wait([drain, gone], return_when=asyncio.FIRST_COMPLETED)
                if not drain.done():
                    # M6: a detached grandchild may hold the pipes after the run
                    # is gone; bound the drain so the run record goes terminal.
                    try:
                        await asyncio.wait_for(asyncio.shield(drain), READER_GRACE_S)
                    except asyncio.TimeoutError:
                        pass
                if not drain.done():
                    for task in readers:
                        task.cancel()
                    await asyncio.gather(*readers, return_exceptions=True)
                    drain.cancel()
                if not gone.done():
                    # Pipes can reach EOF before the exit status is reaped; the
                    # monitor reads proc.returncode next, so let it land (bounded).
                    await asyncio.wait([gone], timeout=READER_GRACE_S)
                gone.cancel()
                return tuple(''.join(captured[n]).encode('utf-8') for n in ('stdout', 'stderr'))
            finally:
                for task in readers:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*readers, return_exceptions=True)
        if payload is not None:
            try:
                return await proc.communicate(input=payload)
            except TypeError:
                job.status, job.exit_code = 'failed', 1
                job.stderr = 'stdin_delivery_failed: communicate rejected input'
                job.events.append({'event': 'stdin_delivery_failed'})
                raise RuntimeError('stdin_delivery_failed: communicate rejected input') from None
        return await proc.communicate()
    task = asyncio.create_task(output())
    refusal_wait = asyncio.ensure_future(refused.wait())
    loop_wait = asyncio.ensure_future(loop_found.wait())
    waiters = [task, refusal_wait, loop_wait]
    ticker = asyncio.ensure_future(gate.run())
    try:
        sample_interval = min(limits['interval'], 30)
        async def sample_cpu():
            # A short review cannot reach the stuck threshold; its hard wall has priority.
            remaining = job.start_time + w.budget() - time.time()
            if (not w.writer and w.budget() <= limits.get('stuck_s', limits['stall_s'] + 300)) or remaining <= 0:
                return None
            try:
                return await asyncio.wait_for(cpu_seconds(runner, job_id, getattr(proc, 'pid', None),
                    int(time.time() / sample_interval)), min(1, remaining))
            except asyncio.TimeoutError:
                return None
        w.decide(time.time(), baseline, job.last_activity_ts,
                 await sample_cpu(),
                 stdout_activity=stdout_at, waiting=getattr(job, '_watchdog_child_active', False))
        while not task.done():
            # Deadlines stay accurate even with the default 120s sample interval.
            delay = min(sample_interval, max(.001, job.start_time + w.budget() - time.time()))
            if limits.get('hard_first_edit') and w.writer and not w.edited and w.edit_baseline_known and w.edit is not None:
                delay = min(delay, max(.001, job.start_time + limits['first_edit_s'] - time.time()))
            if w.writer and w.edit is None:
                delay = min(delay, EDIT_RETRY_S)   # Q7: keep retrying a lost baseline
            done, _ = await asyncio.wait(waiters, timeout=delay, return_when=asyncio.FIRST_COMPLETED)
            if refused.is_set() and not task.done() and job.status in ('cancelled', 'stalled', 'timeout'):
                # Q14/M9/M15: a cancel/stall/timeout verdict is never stomped;
                # the refusal is booked (job.refusal) but the verdict stands.
                # Drop the resolved waiter so the loop keeps sampling normally.
                waiters = [task, loop_wait]
            elif refused.is_set() and not task.done():
                # D-3605 (a): a refused/rate-limited/credit/auth dispatch ends the run now,
                # never 'running' until a stall rule fires; the leg loop records leg_state.
                job.status, job.exit_code = 'failed', 7
                job.events.append({'event': 'dispatch_refused', 'reason': job.refusal})
                logging.getLogger(__name__).info('%s dispatch refused', job_id)
                await runner._reap_process(job_id, proc)
                try:
                    callback = getattr(runner, 'on_sample', None)
                    if callback:
                        callback(job_id)
                except Exception:
                    logging.getLogger(__name__).exception('refusal sample callback failed')
                if job.survivors or not all(isinstance(getattr(proc, n, None), asyncio.StreamReader) for n in ('stdout', 'stderr')):
                    task.cancel(); await asyncio.gather(task, return_exceptions=True)   # kill_incomplete: never wait on a process we may not signal
                    return (await cancel_and_close(task, proc, getattr(job, '_live_log', None)) or (b'', b'')) if job.survivors else await proc.communicate()
                break
            if task in done:
                break
            if loop_found.is_set() and job.status in ('cancelled', 'stalled', 'timeout'):
                waiters = [task] if refused.is_set() else [task, refusal_wait]
            if loop_found.is_set() and job.status not in ('cancelled', 'stalled', 'timeout'):
                event = dict(event='watchdog_stalled', rule='tool_loop')
            else:
                edit = (await asyncio.to_thread(edit_signature, job.cwd or runner.default_cwd)
                        if w.writer else None)
                now = time.time()
                cpu = await sample_cpu()
                event = w.decide(now, edit, job.last_activity_ts, cpu, stdout_activity=stdout_at, waiting=getattr(job, '_watchdog_child_active', False))
            gate.progress(w.progress['edit'])   # REMOVE-AT-R10 (D-3769)
            try:
                callback = getattr(runner, 'on_sample', None)
                if callback:
                    callback(job_id)
            except Exception:
                logging.getLogger(__name__).exception('watchdog sample callback failed')
            if event:
                job.status = 'timeout' if event['rule'] == 'max' else 'stalled'
                job.exit_code = 124 if job.status == 'timeout' else 6
                job.events.append(event)
                job.events.extend([{'event': 'run_timeout'}] if job.status == 'timeout' else [])
                logging.getLogger(__name__).info('%s %s rule=%s', job_id, job.status, event['rule'])
                await runner._reap_process(job_id, proc)
                if job.survivors:   # kill_incomplete: never wait on a process we may not signal
                    return await cancel_and_close(task, proc, getattr(job, '_live_log', None)) or (b'', b'')   # V4: no transport leak on this early return
                if not all(isinstance(getattr(proc, n, None), asyncio.StreamReader) for n in ('stdout', 'stderr')):
                    task.cancel(); await asyncio.gather(task, return_exceptions=True)
                    return await proc.communicate()
                break
        return await gate.finish(runner, job_id, proc, await task)
    finally:
        refusal_wait.cancel(); ticker.cancel(); loop_wait.cancel()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
