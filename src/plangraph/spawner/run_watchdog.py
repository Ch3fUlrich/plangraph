"""Per-run progress decisions, portable sampling, and incremental output capture."""
import asyncio
import codecs
import logging
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time

ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)')
HEADER = re.compile(r'^>\s+\S+\s+·\s+\S+')
SKIP = {'.git', '.pytest_cache', '__pycache__', '.venv', 'logs', 'output', 'node_modules', 'site'}
LIVE_LOG_FLUSH = 16384        # batched appends: one write+fsync per 16 KB, not per line
LIVE_LOG_MAX = 2 * 1024 * 1024
LIVE_LOG_ERRORS = 3           # record at most this many live.log failures per run
READER_GRACE_S = 5.0          # bounded drain once the run is gone (M6)
EDIT_RETRY_S = 15.0           # retry cadence while the edit baseline is unreadable (Q7)


def terminal_record(record, job):
    if job.status not in ('stalled', 'timeout'):
        return False
    record.update(status=job.status, exit_code=job.exit_code, reason=job.status,
                  watchdog=next(e for e in job.events if e['event'].startswith('watchdog_')),
                  events=job.events)
    return True


def prepare_run(service, record, card):
    record['live_log'] = str(service._path(record['run_id']).parent / 'live.log')
    return thresholds(card), record['live_log']


def thresholds(card=None):
    from plangraph.planner.routing import SpawnRefused
    card = card or {}
    result = {}
    for key, default in [('stall_min', 20), ('first_edit_min', 45), ('max_min', 90)]:
        value = card.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise SpawnRefused('invalid watchdog threshold: ' + key)
        result[key.replace('_min', '_s')] = value * 60
    result['hard_max'] = False   # table max_min may carry the first-edit hold (M5)
    timeout = os.environ.get('PLANGRAPH_SPAWN_RUN_TIMEOUT')
    if timeout and 'max_min' not in card:
        try:
            value = float(timeout)
            if not math.isfinite(value) or value <= 0:
                raise ValueError
        except ValueError:
            # M17: a bad env knob is refused before launch (exit 2), like a bad card.
            raise SpawnRefused('invalid run timeout: PLANGRAPH_SPAWN_RUN_TIMEOUT') from None
        result['max_s'] = value
        # An operator-set run timeout is a wall clock: the first-edit hold
        # never extends it (M5's hold 'does not extend the max deadline').
        result['hard_max'] = True
    interval = os.environ.get('PLANGRAPH_SPAWN_WATCHDOG_INTERVAL')
    try:
        result['interval'] = float(interval) if interval else min(120, result['stall_s'] / 4)
    except ValueError:
        raise SpawnRefused('invalid watchdog interval') from None
    if not math.isfinite(result['interval']) or result['interval'] <= 0:
        raise SpawnRefused('invalid watchdog interval')
    return result


def is_activity(line):
    line = ANSI.sub('', line).strip()
    return bool(line and not HEADER.match(line))


class Watchdog:
    def __init__(self, limits, start, edit, writer=True):
        # Non-writer roles never edit and may buffer output: only 'max' applies.
        self.limits, self.start, self.edit, self.writer = limits, start, edit, writer
        self.edited = False
        self.cpu = None
        self.progress = dict(edit=start, activity=start, cpu=start)

    def budget(self):
        """Hard deadline in seconds. A pre-edit writer whose edits we can see is
        owed its first-edit window on top of the table run budget (M5's 'holds
        while output flows' ends at max_s + first_edit_s); once edited, or when
        the edit signal is unavailable, or for non-writers, max_s ends the run.
        An operator-set PLANGRAPH_SPAWN_RUN_TIMEOUT is a wall clock and is
        never extended."""
        if self.limits.get('hard_max'):
            return self.limits['max_s']
        return self.limits['max_s'] + (self.limits['first_edit_s']
                                       if self.writer and not self.edited and self.edit is not None
                                       else 0)

    def decide(self, now, edit, activity, cpu):
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
        elif not self.writer:
            pass
        elif not self.edited:
            if edit is None:
                # Q7/M4: edits are invisible this tick; no rule may assert
                # 'no worktree edit' (max is the backstop).
                pass
            elif activity is not None and now - activity >= stall:
                # Q6: no activity signal (buffered harness) is not a stall.
                rule = 'no-activity'
            elif now - self.start >= self.limits['first_edit_s'] and now - (
                    activity if activity is not None else self.start) >= stall:
                # no-first-edit = no output AND no edit (M5/Q6, goal 1).
                rule = 'no-first-edit'
        elif now - max(self.progress.values()) >= stall:
            rule = 'no-progress'
        if rule:
            return dict(event='watchdog_timeout' if rule == 'max' else 'watchdog_stalled',
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
                              capture_output=True, timeout=10, check=False)
        return (count, newest, size), head.stdout if head.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def proc_scan():
    result = {}
    for path in Path('/proc').iterdir():
        if not path.name.isdigit():
            continue
        try:
            fields = (path / 'stat').read_text().rsplit(')', 1)[1].split()
            result[int(path.name)] = (int(fields[1]), int(fields[3]),
                                      sum(int(fields[i]) for i in (11, 12, 13, 14)))
        except (OSError, ValueError, IndexError):
            continue
    return result


async def cpu_seconds(runner, job_id, pid, tick):
    try:
        handle = getattr(runner, '_win_jobs', {}).get(job_id)
        if handle is not None:
            return runner.win_job_api.cpu_seconds(handle)
        if not sys.platform.startswith('linux') or not pid:
            return None
        cache = getattr(runner, '_watchdog_cpu_cache', None)
        if cache is None or cache[0] != tick:
            # Cache the task before awaiting: concurrent monitors share ONE scan.
            cache = runner._watchdog_cpu_cache = (tick, asyncio.create_task(asyncio.to_thread(proc_scan)))
        rows = await cache[1]
        def belongs(child):
            seen = set()
            while child in rows and child not in seen:
                if child == pid or rows[child][1] == pid:
                    return True
                seen.add(child)
                child = rows[child][0]
            return False
        return sum(row[2] for child, row in rows.items() if belongs(child)) / os.sysconf('SC_CLK_TCK')
    except Exception:
        return None


async def run_gone(proc):
    """Resolve once the run has exited. ``Process.wait()`` cannot signal this:
    asyncio resolves its waiters only after every pipe transport disconnects
    (CPython base_subprocess._try_finish), so a detached grandchild holding the
    pipes wedges it forever although the return code is already known (M6)."""
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
    from .env_redact import redact_env_values
    job = runner.jobs[job_id]
    env = runner._child_envs.get(job_id, os.environ)
    limits = getattr(job, '_watchdog_limits', None) or thresholds()
    baseline = getattr(job, '_watchdog_baseline', None)
    w = Watchdog(limits, job.start_time, baseline, writer=job.role in ('implement', 'fix'))
    captured = {'stdout': [], 'stderr': []}
    def scrub(text):
        return redact(redact_env_values(text, env))
    async def reader(stream, name):
        pending, tail = '', ''
        log = LiveLog(job)
        decoder = codecs.getincrementaldecoder('utf-8')('replace')
        def consume(text):
            nonlocal pending, tail
            if not text:
                return
            captured[name].append(text)   # raw; kept even if a drain grace cancels us
            pending += text
            lines = pending.splitlines(keepends=True)
            pending = ''
            active = False
            for line in lines:
                if not line.endswith(('\n', '\r')):
                    pending = line
                    continue
                if is_activity(line):
                    active = True
            if active:
                job.last_activity_ts = time.time()
            scrubbed = scrub(text)      # once per chunk: linear (Q4/M3)
            tail = (tail + scrubbed)[-4096:]
            setattr(job, name, tail)
            log.feed(scrubbed)
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
            log.flush()   # also on drain-grace cancellation: batched bytes land
    async def output():
        payload = runner._stdin_payloads.get(job_id)
        if all(isinstance(getattr(proc, n, None), asyncio.StreamReader) for n in ('stdout', 'stderr')):
            readers = [asyncio.create_task(reader(getattr(proc, n), n)) for n in ('stdout', 'stderr')]
            try:
                if payload is not None and proc.stdin is not None:
                    try:
                        proc.stdin.write(payload)
                        await proc.stdin.drain()
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    finally:
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
                pass
        return await proc.communicate()
    task = asyncio.create_task(output())
    try:
        while not task.done():
            # Deadlines stay accurate even with the default 120s sample interval.
            delay = min(limits['interval'], max(.001, job.start_time + w.budget() - time.time()))
            if w.edit is None:
                delay = min(delay, EDIT_RETRY_S)   # Q7: keep retrying a lost baseline
            done, _ = await asyncio.wait([task], timeout=delay)
            if done:
                break
            edit = await asyncio.to_thread(edit_signature, job.cwd or runner.default_cwd)
            now = time.time()
            cpu = (await cpu_seconds(runner, job_id, getattr(proc, 'pid', None), int(now / limits['interval']))
                   if now - job.start_time < limits['max_s'] else None)
            event = w.decide(now, edit, job.last_activity_ts, cpu)
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
                if job.status == 'timeout':
                    job.events.append({'event': 'run_timeout'})
                logging.getLogger(__name__).info('%s %s rule=%s', job_id, job.status, event['rule'])
                await runner._reap_process(job_id, proc)
                if not all(isinstance(getattr(proc, n, None), asyncio.StreamReader) for n in ('stdout', 'stderr')):
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    return await proc.communicate()
                break
        return await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
