"""Select the newest main commit satisfying required branch checks."""
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from plangraph.spawner.private_io import atomic_write_private


def release_path():
    return Path.home() / '.local/state/plangraph/release.json'


def git(*args):
    return subprocess.check_output(['git', *args], text=True, timeout=30).strip()


def gh_query(endpoint):
    return json.loads(subprocess.check_output(
        ['gh', 'api', endpoint], text=True, timeout=30))


def record(query=gh_query, candidates=None, path=None):
    required = query('repos/{owner}/{repo}/branches/main/protection/required_status_checks')
    names = set(required.get('contexts', [])) | {c['context'] for c in required.get('checks', [])}
    if not names:
        raise ValueError('no required CI checks configured')
    for sha in candidates if candidates is not None else git('rev-list', 'origin/main').splitlines():
        runs = query(f'repos/{{owner}}/{{repo}}/commits/{sha}/check-runs?per_page=100')['check_runs']
        statuses = query(f'repos/{{owner}}/{{repo}}/commits/{sha}/status')['statuses']
        checks = {}
        # API order is newest first; never let an old success hide a new failure.
        for run in runs:
            checks.setdefault(run['name'], run['conclusion'] if run['status'] == 'completed' else 'pending')
        for status in statuses:
            checks.setdefault(status['context'], status['state'])
        if all(checks.get(name) == 'success' for name in names):
            data = {'sha': sha, 'recorded_at': datetime.now(UTC).isoformat(),
                    'checks': {name: checks[name] for name in sorted(names)}}
            atomic_write_private(path or release_path(), json.dumps(data, indent=2) + '\n')
            return data
    raise ValueError('no main commit has green required CI checks')
