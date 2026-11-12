"""Select the newest main commit satisfying required branch checks."""
import json
import subprocess
from datetime import UTC, datetime
from itertools import islice
from pathlib import Path

from plangraph.routing_support.private_io import atomic_write_private


def release_path():
    return Path.home() / '.local/state/plangraph/release.json'


def git(*args):
    return subprocess.check_output(['git', *args], text=True, timeout=30).strip()


def gh_query(endpoint):
    return json.loads(subprocess.check_output(
        ['gh', 'api', endpoint], text=True, timeout=30))


def record(query=gh_query, candidates=None, path=None, max_commits=50):
    if max_commits < 1:
        raise ValueError('max_commits must be positive')
    required = query('repos/:owner/:repo/branches/main/protection/required_status_checks')
    names = set(required.get('contexts', [])) | {c['context'] for c in required.get('checks', [])}
    if not names:
        raise ValueError('no required CI checks configured')
    if candidates is None:
        candidates = git('rev-list', f'--max-count={max_commits}', 'origin/main').splitlines()
    for sha in islice(candidates, max_commits):
        runs = query(f'repos/:owner/:repo/commits/{sha}/check-runs?per_page=100')['check_runs']
        statuses = query(f'repos/:owner/:repo/commits/{sha}/status')['statuses']
        checks = {}
        results = []
        for run in runs:
            when = max(run.get('completed_at') or '', run.get('started_at') or '')
            state = run['conclusion'] if run['status'] == 'completed' else 'pending'
            results.append((when, run['name'], state))
        for status in statuses:
            when = max(status.get('updated_at') or '', status.get('created_at') or '')
            results.append((when, status['context'], status['state']))
        # Sort independently of API order. On timestamp ties, fail closed.
        for _, name, state in sorted(results, key=lambda r: (r[0], r[2] != 'success')):
            checks[name] = state
        if all(checks.get(name) == 'success' for name in names):
            data = {'sha': sha, 'recorded_at': datetime.now(UTC).isoformat(),
                    'checks': {name: checks[name] for name in sorted(names)}}
            atomic_write_private(path or release_path(), json.dumps(data, indent=2) + '\n')
            return data
    raise ValueError('no main commit has green required CI checks')
