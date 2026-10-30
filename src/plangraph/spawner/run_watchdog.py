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
    timeout = os.environ.get('PLANGRAPH_SPAWN_RUN_TIMEOUT')
    if timeout and 'max_min' not in card:
        try:
            value = float(timeout)
            if not math.isfinite(value) or value <= 0:
                raise ValueError
        except ValueError:
            raise ValueError('invalid run timeout: PLANGRAPH_SPAWN_RUN_TIMEOUT') from None
        result['max_s'] = value
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
    def __init__(self, limits, start, edit):
        self.limits, self.start, self.edit = limits, start, edit
        self.edited = False
        self.cpu = None
        self.progress = dict(edit=start, activity=start, cpu=start)

    def decide(self, now, edit, activity, cpu):
        if edit != self.edit and edit is not None and self.edit is not None:
            self.edited = True
            self.progress['edit'] = now
        if edit is not None:
            self.edit = edit
        if activity is not None:
            self.progress['activity'] = activity
        if self.edited and cpu is not None and self.cpu is not None and cpu > self.cpu:
            self.progress['cpu'] = now
        self.cpu = cpu
        stall = self.limits['stall_s']
        rule = None
        if now - self.start >= self.limits['max_s']:
            rule = 'max'
        elif not self.edited:
            if now - (activity if activity is not None else self.start) >= stall:
                rule = 'no-activity'
            elif now - self.start >= self.limits['first_edit_s']:
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


async def collect(runner, job_id, proc):
    from .runner import redact
    from .env_redact import redact_env_values
    from .private_io import append_private, atomic_write_private
    job = runner.jobs[job_id]
    env = runner._child_envs.get(job_id, os.environ)
    limits = getattr(job, '_watchdog_limits', None) or thresholds()
    baseline = getattr(job, '_watchdog_baseline', None)
    w = Watchdog(limits, job.start_time, baseline)
    def scrub(text):
        return redact(redact_env_values(text, env))
    async def reader(stream, name):
        chunks, pending = [], ''
        decoder = codecs.getincrementaldecoder('utf-8')('replace')
        async def consume(text):
            nonlocal pending
            chunks.append(text)
            pending += text
            lines = pending.splitlines(keepends=True)
            pending = ''
            for line in lines:
                if not line.endswith(('\n', '\r')):
                    pending = line
                    continue
                if is_activity(line):
                    job.last_activity_ts = time.time()
                path = getattr(job, '_live_log', None)
                if path:
                    append_private(path, scrub(line))
                    if Path(path).stat().st_size > 2 * 1024 * 1024:
                        data = Path(path).read_bytes()[-1024 * 1024:]
                        atomic_write_private(path, data.decode('utf-8', 'replace'))
            setattr(job, name, scrub(''.join(chunks))[-4096:])
        while True:
            data = await stream.read(65536)
            if not data:
                break
            await consume(decoder.decode(data))
        await consume(decoder.decode(b'', final=True))
        if pending:
            if is_activity(pending):
                job.last_activity_ts = time.time()
            path = getattr(job, '_live_log', None)
            if path:
                append_private(path, scrub(pending))
        return ''.join(chunks).encode('utf-8')
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
                result = await asyncio.gather(*readers)
                await proc.wait()
                return result
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
            # Max deadlines remain accurate even with the default 120s sample interval.
            delay = min(limits['interval'], max(.001, job.start_time + limits['max_s'] - time.time()))
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
