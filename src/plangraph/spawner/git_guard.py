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
                                capture_output=True, text=True, check=True)
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
    if role not in ('implement', 'fix'):
        return prompt, {}
    name = model + ' (plangraph writer)'
    email = run_id + '@writer.plangraph.invalid'
    return (prompt + '\nEnd every commit message with the trailer line: Writer: '
            + model + ' ' + run_id,
            {'GIT_AUTHOR_NAME': name,
             'GIT_AUTHOR_EMAIL': email,
             'GIT_COMMITTER_NAME': name,
             'GIT_COMMITTER_EMAIL': email})
