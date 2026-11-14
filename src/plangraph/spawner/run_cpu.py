"""Process-tree CPU accounting for the run watchdog."""
import asyncio
import os
import sys
from pathlib import Path


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
        rows = await asyncio.shield(cache[1])
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
