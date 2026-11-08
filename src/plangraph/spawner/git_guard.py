"""Shared-config protection and per-leg writer identity.

The config file under ``--git-common-dir`` is shared by every worktree and
every sibling lane.  The guard therefore never treats a key it cannot
attribute as a mutation: only keys this run can be shown to have added (they
appear in the spawn-time baseline diff) *and* that are dangerous count.
Danger is a denylist (command execution, credential redirection, config
injection); every other key a sibling lane or the user writes is recorded at
most and never removed or rolled back.  Offending keys are removed with
``git config --file <path> --unset-all`` and pre-existing values restored
per key - git's own ``config.lock`` read-modify-write.  The file is never
rewritten from a snapshot.  Errors listing the config only record a warning
event: the run is neither failed nor restored.
"""
from pathlib import Path
import re
import subprocess
import tempfile

# Denylist of dangerous keys (git prints section and key lower-cased, so the
# pattern is matched against the lower-cased key; the original spelling is
# kept for --unset-all since subsections are case-sensitive).
DANGEROUS = re.compile(
    r'core\.(?:hookspath|sshcommand|askpass|fsmonitor|editor|pager|gitproxy|alternaterefscommand)'
    r'|sequence\.editor|diff\.external'
    r'|pager\..*|alias\..*|ssh\.variant'
    r'|(?:diff|merge|filter|mergetool|difftool|remote)\..+\.(?:command|textconv|driver|cmd|clean|smudge|process|uploadpack|receivepack|proxy)'
    r'|uploadpack\.packobjectshook|http\.proxy'
    r'|gpg(?:\..+)?\.program'
    r'|credential\..*'
    r'|url\..+\.(?:push)?insteadof'
    r'|include(?:if)?\..*')


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
                                capture_output=True, text=True, encoding="utf-8", errors="replace", check=True)
        path = (Path(work_dir) / result.stdout.strip()).resolve() / 'config'
        baseline = None
        # Close before git opens it, including on Windows.
        with tempfile.TemporaryDirectory(prefix='plangraph-git-guard-') as directory:
            private = Path(directory) / 'config'
            private.write_bytes(path.read_bytes())
            private.chmod(0o600)
            try:
                baseline = _list(private)
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
        return path, baseline
    except (OSError, subprocess.SubprocessError):
        return None


def check_guard(guard, job):
    if not guard or job is None:
        return
    path, baseline = guard
    if baseline is None:
        job.events.append({'event': 'git_config_warning', 'reason': 'baseline_unavailable'})
        return
    try:
        current = _list(path)
    except (OSError, ValueError, subprocess.SubprocessError):
        job.events.append({'event': 'git_config_warning', 'reason': 'config_list_failed'})
        return
    changed = sorted(k for k in baseline.keys() | current.keys()
                     if sorted(baseline.get(k, [])) != sorted(current.get(k, [])))
    # Mutation = a dangerous key that gained values during this run's window.
    offending = [k for k in changed if DANGEROUS.fullmatch(k.lower())
                 and set(current.get(k, [])) - set(baseline.get(k, []))]
    for key in offending:
        try:
            result = subprocess.run(['git', 'config', '--file', str(path), '--unset-all', key],
                                    capture_output=True)
            if result.returncode not in (0, 5):
                raise ValueError('unset failed')
            for value in baseline.get(key, []):
                subprocess.run(['git', 'config', '--file', str(path), '--add', key, value],
                               capture_output=True, check=True)
        except (OSError, ValueError, subprocess.SubprocessError):
            job.events.append({'event': 'git_config_warning',
                               'reason': 'config_unset_failed', 'keys': [key]})
    if offending:
        job.events.append({'event': 'git_config_mutated', 'keys': sorted(offending)})
    benign = [k for k in changed if k not in set(offending)]
    if benign:
        job.events.append({'event': 'git_config_changed', 'keys': benign})
    if offending:
        job.status = 'failed'
        job.exit_code = 1


def writer_launch(prompt, role, model, run_id):
    if role in ('review', 'research'):
        return prompt, {}
    name = model + ' (plangraph writer)'
    email = run_id + '@writer.plangraph.invalid'
    return (prompt + '\nEnd every commit message with the trailer line: Writer: '
            + model + ' ' + run_id,
            {'GIT_AUTHOR_NAME': name,
             'GIT_AUTHOR_EMAIL': email,
             'GIT_COMMITTER_NAME': name,
              'GIT_COMMITTER_EMAIL': email})


def writer_completion(job):
    """The runner, not its caller or child's prose, owns writer publication."""
    if job.role in ('review', 'research') or job.status != 'completed':
        return
    import sys
    if not (Path(job.cwd or '.') / 'tools/preflight.py').exists():
        job.events.append(dict(event='writer_preflight_skipped', reason='no_preflight_configured'))
        return
    try:
        result = subprocess.run(
            [sys.executable, 'tools/preflight.py', '--writer-completion'],
            cwd=job.cwd, capture_output=True, text=True, timeout=1800)
        failed = result.returncode != 0
        reason = result.stdout.strip() or 'HOLD preflight_unavailable'
    except (OSError, subprocess.SubprocessError):
        failed, reason = True, 'HOLD preflight_unavailable'
    if failed:
        job.status, job.exit_code = 'hold', 1
        job.stderr = reason
        job.events.append(dict(event='writer_hold', reason=reason))


def admit_brief(prompt, role, header):
    brief = prompt.removeprefix(header).split('\nEnd every commit message with the trailer line: Writer: ', 1)[0]
    if role in ('implement', 'review') and not brief.strip():
        from plangraph.routing_errors import SpawnRefused
        raise SpawnRefused('prompt_empty')


def collect_brief_tests(prompt, role, cwd):
    """Collect named tests in the launch worktree before admitting a writer."""
    if role in ('review', 'research'):
        return
    import sys
    tests = list(dict.fromkeys(re.findall(
        r'(?<![\w/])((?:tests/)[\w./-]+\.py(?:::[\w-]+)*)', prompt)))
    if not tests:
        return
    from plangraph.routing_errors import SpawnRefused
    try:
        result = subprocess.run(
            [sys.executable, '-m', 'pytest', '--collect-only', '-q', *tests],
            cwd=cwd, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SpawnRefused(f'test_env_unready: {exc}') from exc
    if result.returncode:
        raise SpawnRefused('test_env_unready: ' + result.stdout + result.stderr)
