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
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile

CLOSED_LAYERS = {'OPENCODE_DISABLE_PROJECT_CONFIG': '1', 'OPENCODE_DISABLE_CLAUDE_CODE': '1',
                 'OPENCODE_DISABLE_CLAUDE_CODE_SKILLS': '1'}
WRITER_ROLES = ('implement', 'fix')

#: The one provider whose credential an isolated review child gets (D-4196).
GO_PROVIDER = 'opencode-go'

#: Override for the *source* credential store the installer reads (hermetic
#: tests point it at a fabricated store; production reads the invoking
#: account's store -- root resolves root's empty store and gets a clean
#: credential-absent answer, never root's or another account's credential).
GO_AUTH_DB_ENV = 'PLANGRAPH_OPENCODE_GO_AUTH_DB'

#: The clean refusal when no credential can be installed; shaped for
#: env_redact.unavailable_reason so the run ends not_served, never a crash.
GO_ABSENT_REASON = 'not_served_on_host: opencode-go credential absent (D-4196)'


def go_source_db(environ=None):
    """Read-only resolution of the source opencode credential store, or None.

    Never raises: the explicit override wins, else the invoking account's
    ``~/.local/share/opencode/opencode.db``.
    """
    env = os.environ if environ is None else environ
    source = env.get(GO_AUTH_DB_ENV)
    if source:
        return Path(source)
    if env.get('XDG_DATA_HOME'):
        return Path(env['XDG_DATA_HOME']) / 'opencode' / 'opencode.db'
    home = env.get('HOME')
    return (Path(home) / '.local' / 'share' / 'opencode' / 'opencode.db') if home else None


def install_go_credential(job_dir, source=None):
    """D-4196: give the isolated review child the opencode-go credential -- nothing else.

    Review isolation (D-3552) points HOME/XDG_DATA_HOME at the per-job dir,
    which hides the account's credentials. Model discovery is supplied by
    OpenCode itself, not copied account table data. Restoring the host data dir is exactly the leak the
    isolation prevents, so this writes a *fresh, job-owned* store at
    ``<job_dir>/opencode/opencode.db`` (the review child's
    ``XDG_DATA_HOME/opencode``) holding the schema and only the selected provider auth
    row -- the active opencode-go one.  Nothing else is copied: no accounts,
    no sessions, no other provider's credential, no host key material.  The
    file is 0600 inside the job dir the spawner already removes with the job.

    True iff the store was written.  False -- never raises -- when the
    credential is absent or unreadable: the leg then ends ``not_served`` via
    the host probe, never a crash.  Key bytes touch only the 0600 file: never
    the child env, an event, or a log line.
    """
    src = source if source is not None else go_source_db()
    dst_dir = Path(job_dir) / 'opencode'
    dst = dst_dir / 'opencode.db'
    temporary = None
    try:
        if src is None or not src.is_file():
            return False
        dst_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(dst_dir, 0o700)
        read = sqlite3.connect(Path(src).resolve().as_uri() + '?mode=ro', uri=True)
        write = None
        try:
            schema = [row[0] for row in read.execute(
                "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"
                " AND name NOT GLOB 'sqlite_*' AND tbl_name IN ('migration', 'credential')"
                " AND type IN ('table', 'index')"
                " ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END, name")]
            migrations = read.execute(
                'SELECT id, time_completed FROM migration ORDER BY id').fetchall()
            creds = read.execute(
                'SELECT id, integration_id, label, value, connector_id, method_id,'
                ' active, time_created, time_updated FROM credential'
                ' WHERE integration_id = ? AND active = 1', (GO_PROVIDER,)).fetchall()
            if not creds:
                return False
            fd, name = tempfile.mkstemp(prefix='pg-auth-', suffix='.db', dir=dst_dir)
            temporary = Path(name)
            os.close(fd)
            write = sqlite3.connect(temporary)
            for statement in schema:
                write.execute(statement)
            write.executemany('INSERT OR IGNORE INTO migration(id, time_completed) VALUES(?, ?)', migrations)
            write.executemany(
                'INSERT INTO credential(id, integration_id, label, value, connector_id,'
                ' method_id, active, time_created, time_updated) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)', creds)
            write.commit()
        finally:
            read.close()
            if write is not None:
                write.close()
        os.replace(temporary, dst)
        return True
    except (OSError, sqlite3.Error):
        return False
    finally:
        if temporary is not None:
            for suffix in ('', '-journal', '-wal', '-shm'):
                Path(str(temporary) + suffix).unlink(missing_ok=True)


def review_credential_reason(model, job_dir, child_env, reason=None):
    """Install only Go auth for an isolated review; fail closed if absent."""
    child_env['XDG_DATA_HOME'] = job_dir
    if model.startswith('opencode-go/') and not install_go_credential(job_dir):
        return GO_ABSENT_REASON
    return reason


def close_config_layers(child_env):
    """Point every opencode config layer at a fresh private dir; returns the dir (caller removes it)."""
    oc_dir = tempfile.mkdtemp(prefix='pg-ocdata-')   # mode 0700
    empty = Path(oc_dir) / 'pg-empty.json'
    empty.write_text('{}')
    empty.chmod(0o600)
    for name, value in dict(CLOSED_LAYERS, OPENCODE_CONFIG_DIR=oc_dir, OPENCODE_CONFIG=str(empty)).items():
        child_env[name] = value   # item by item: a mapping may police each key
    return oc_dir


def tree_state(work_dir):
    """Hash HEAD and status; None outside a git work tree.

    An unborn HEAD (fresh repo) hashes as ``unborn``: the status still counts.
    """
    def git(*args, check=True):
        return subprocess.run(['git', '-C', str(work_dir), *args], capture_output=True,
                              timeout=30, check=check)
    try:
        status = git('status', '--porcelain=v1', '-uall').stdout
        head = git('rev-parse', '--verify', '-q', 'HEAD', check=False)
        return hashlib.sha256((head.stdout if head.returncode == 0 else b'unborn')
                              + b'\0' + status).hexdigest()
    except (OSError, subprocess.SubprocessError):
        return None


#: Run exit code of a writer that changed nothing; distinct from 4 (exhausted legs).
WRITER_NO_CHANGES_EXIT = 5


UNCHECKED = {'event': 'writer_change_unchecked', 'reason': 'cwd shared with a live writer'}


def writer_commits(work_dir, email):
    """Include linked-worktree refs and reflogs, independent of commit dates."""
    commits = set()
    for identity in ('author', 'committer'):
        result = subprocess.run(
            ['git', '-C', str(work_dir), 'log', '--all', '--reflog',
             '--format=%H', '--fixed-strings', '--' + identity + '=<' + email + '>'],
            capture_output=True, timeout=30, check=True)
        commits.update(result.stdout.decode('ascii').split())
    return commits


def record_baseline(job, work_dir, live_jobs=()):
    """Hash the tree before a writer starts. A cwd shared with another live writer leaves
    both unchecked (a whole-tree hash cannot tell their changes apart) and says so."""
    if job.role not in WRITER_ROLES:
        return
    sharers = [o for o in live_jobs if o.role in WRITER_ROLES and o.cwd == job.cwd
               and o.status == 'running' and o.job_id != job.job_id]
    if sharers:
        for o in [job, *sharers]:
            if UNCHECKED not in o.events:
                o.events.append(dict(UNCHECKED))
        return
    state = tree_state(work_dir) if work_dir else None
    if state:
        baseline = {'event': 'writer_baseline', 'state': state}
        job.events.append(baseline)
        email = getattr(job, 'writer_email', None)
        if email:
            try:
                baseline['commits'] = sorted(writer_commits(work_dir, email))
            except (OSError, subprocess.SubprocessError):
                job.events.append({'event': 'writer_change_unchecked',
                                   'reason': 'writer commit lookup failed'})


def fail_unchanged_writer(job, work_dir):
    """A writer that exits 0 without a commit or a tree change ends failed, not completed."""
    base = next((e['state'] for e in job.events if e.get('event') == 'writer_baseline'), None)
    if (job.role not in WRITER_ROLES or base is None or job.status != 'completed'
            or UNCHECKED in job.events or tree_state(work_dir) != base):
        return
    email = getattr(job, 'writer_email', None)
    if not email:
        job.events.append({'event': 'writer_change_unchecked',
                           'reason': 'writer identity missing'})
        return
    baseline = next(e for e in job.events if e.get('event') == 'writer_baseline')
    if 'commits' not in baseline:
        job.events.append({'event': 'writer_change_unchecked',
                           'reason': 'writer commit lookup failed'})
        return
    try:
        if writer_commits(work_dir, email) - set(baseline['commits']):
            return
    except (OSError, subprocess.SubprocessError):
        job.events.append({'event': 'writer_change_unchecked',
                           'reason': 'writer commit lookup failed'})
        return
    job.status = 'failed'
    job.exit_code = WRITER_NO_CHANGES_EXIT
    job.events.append({'event': 'writer_no_changes'})
    job.stderr = 'writer_no_changes: exit 0 with no commit and no working-tree change\n' + (job.stderr or '')
