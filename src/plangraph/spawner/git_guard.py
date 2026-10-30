"""Shared-config protection and per-leg writer identity."""
from pathlib import Path
import re
import subprocess
import tempfile


def _list(path):
    result = subprocess.run(['git', 'config', '--file', str(path), '--no-includes',
                             '--list', '-z'], capture_output=True, check=True)
    values = {}
    for item in result.stdout.decode('utf-8', errors='surrogateescape').split('\0'):
        if item:
            key, _, value = item.partition('\n')
            values.setdefault(key, []).append(value)
    return values


def init_guard(work_dir):
    if not work_dir:
        return None
    try:
        result = subprocess.run(['git', '-C', str(work_dir), 'rev-parse', '--git-common-dir'],
                                capture_output=True, text=True, check=True)
        path = (Path(work_dir) / result.stdout.strip()).resolve() / 'config'
        original = path.read_bytes()
        baseline = None
        # Close before git opens it, including on Windows.
        with tempfile.TemporaryDirectory(prefix='plangraph-git-guard-') as directory:
            private = Path(directory) / 'config'
            private.write_bytes(original)
            private.chmod(0o600)
            try:
                baseline = _list(private)
            except (OSError, subprocess.SubprocessError):
                pass
        return path, original, baseline
    except (OSError, subprocess.SubprocessError):
        return None


def check_guard(guard, job):
    if not guard or job is None:
        return
    path, original, baseline = guard
    keys = []
    try:
        if path.read_bytes() == original:
            return
        if baseline is None:
            raise ValueError('unparseable baseline')
        current = _list(path)
        changed = sorted(k for k in baseline.keys() | current.keys()
                         if baseline.get(k) != current.get(k))
        keys = [k for k in changed if not re.fullmatch(r'branch\..+\.[^.]+|remote\.[^.]+\.fetch', k)]
        if not keys:
            if changed:
                job.events.append({'event': 'git_config_changed', 'keys': changed})
            return
        for key in keys:
            result = subprocess.run(['git', 'config', '--file', str(path), '--unset-all', key],
                                    capture_output=True)
            if result.returncode not in (0, 5):
                raise ValueError('unset failed')
            for value in baseline.get(key, []):
                subprocess.run(['git', 'config', '--file', str(path), '--add', key, value],
                               capture_output=True, check=True)
    except (OSError, ValueError, subprocess.SubprocessError):
        try:
            path.write_bytes(original)
        except OSError:
            pass
    job.events.append({'event': 'git_config_mutated', 'keys': keys})
    job.status = 'failed'
    job.exit_code = 1


def writer_launch(prompt, role, model, run_id):
    if role not in ('implement', 'fix'):
        return prompt, {}
    return (prompt + '\nEnd every commit message with the trailer line: Writer: '
            + model + ' ' + run_id,
            {'GIT_AUTHOR_NAME': model + ' (plangraph writer)',
             'GIT_AUTHOR_EMAIL': run_id + '@writer.plangraph.invalid'})
