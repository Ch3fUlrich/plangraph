"""Per-run opencode config isolation and the writer no-change check (D-3236).

opencode's global config dir is ``OPENCODE_CONFIG_DIR`` or else
``$XDG_CONFIG_HOME/opencode`` (``~/.config/opencode``, Windows included). A
writer that loaded the operator's global config got its ``instructions`` (an
AutoOS leaf contract that forbids commits) and exited 0 with no change
(run-fcf3bb7d). Every opencode run now gets a spawner-owned config dir and an
empty ``OPENCODE_CONFIG``, with project and Claude Code layers off; providers
come only from ``OPENCODE_CONFIG_CONTENT`` (the overlay). A writer keeps HOME
and its data dir (auth, git); review adds its own store and tool deny.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess
import tempfile

CLOSED_LAYERS = {'OPENCODE_DISABLE_PROJECT_CONFIG': '1', 'OPENCODE_DISABLE_CLAUDE_CODE': '1',
                 'OPENCODE_DISABLE_CLAUDE_CODE_SKILLS': '1'}


def close_config_layers(child_env):
    """Point every opencode config layer at a fresh private dir; returns the dir (caller removes it)."""
    oc_dir = tempfile.mkdtemp(prefix='pg-ocdata-')   # mode 0700
    empty = Path(oc_dir) / 'pg-empty.json'
    empty.write_text('{}')
    for name, value in dict(CLOSED_LAYERS, OPENCODE_CONFIG_DIR=oc_dir, OPENCODE_CONFIG=str(empty)).items():
        child_env[name] = value   # item by item: a mapping may police each key
    return oc_dir


def tree_state(work_dir):
    """Hash of HEAD plus the full porcelain status; None outside a git work tree.

    An unborn HEAD (fresh repo) hashes as ``unborn``: the status still counts.
    """
    def git(*args, check=True):
        return subprocess.run(['git', '-C', str(work_dir), *args], capture_output=True,
                              timeout=30, check=check)
    try:
        status = git('status', '--porcelain=v1', '-uall').stdout
        head = git('rev-parse', '--verify', '-q', 'HEAD', check=False)
        return hashlib.sha256((head.stdout if head.returncode == 0 else b'unborn') + b'\0' + status).hexdigest()
    except (OSError, subprocess.SubprocessError):
        return None


def record_baseline(job, work_dir):
    state = tree_state(work_dir) if work_dir else None
    if state:
        job.events.append({'event': 'writer_baseline', 'state': state})


def fail_unchanged_writer(job, work_dir):
    """A writer that exits 0 without a commit or a tree change ends failed, not completed."""
    base = next((e['state'] for e in job.events if e.get('event') == 'writer_baseline'), None)
    if base is None or job.status != 'completed' or tree_state(work_dir) != base:
        return
    job.status = 'failed'
    job.exit_code = 4
    job.events.append({'event': 'writer_no_changes'})
    job.stderr = 'writer_no_changes: exit 0 with no commit and no working-tree change\n' + (job.stderr or '')
