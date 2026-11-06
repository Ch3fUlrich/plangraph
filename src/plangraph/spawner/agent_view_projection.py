"""Shared public fleet projection, independent of the spawner runtime."""
from datetime import datetime
import json
from pathlib import Path

FIELDS = ('host', 'name', 'runtime', 'model', 'state', 'item', 'age_s')


def read(path):
    value = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError('expected object')
    return value


def project(source, host, runtime=None, now=None):
    """Whitelist scalars, including when reading a remote result."""
    row = {k: source.get(k) if isinstance(source.get(k), (str, int, float))
           and not isinstance(source.get(k), bool) else None for k in FIELDS}
    row['host'] = host
    row['runtime'] = runtime or (row['runtime'] if row['runtime'] in
                               ('claude', 'opencode', 'spawner-run') else None)
    if now is not None:
        for field, alias in [('name', 'run_id'), ('item', 'item_key')]:
            if row[field] is None and isinstance(source.get(alias), str):
                row[field] = source[alias]
        row['state'] = source.get('state', source.get('status'))
        if not isinstance(row['state'], str):
            row['state'] = None
        stamp = source.get('started', source.get('launched_ts', source.get('createdAt')))
        try:
            if isinstance(stamp, str):
                stamp = datetime.fromisoformat(stamp.replace('Z', '+00:00')).timestamp()
            row['age_s'] = max(0, now - float(stamp))
        except (ValueError, TypeError, OverflowError):
            row['age_s'] = None
    return row


def unavailable(host, reason):
    return dict(host=host, readable=False, reason=reason, agents=[])


def project_view(data):
    """Isolate malformed hosts and rows while enforcing the public schema."""
    hosts = []
    for entry in data['hosts']:
        entry = entry if isinstance(entry, dict) else {}
        host = entry.get('host') if isinstance(entry.get('host'), str) else 'unknown'
        rows = entry.get('agents')
        if entry.get('readable') is not True or not isinstance(rows, list):
            hosts.append(unavailable(host, 'unreadable'))
        else:
            hosts.append(dict(host=host, readable=True, reason=None,
                              agents=[project(r if isinstance(r, dict) else {}, host) for r in rows]))
    return dict(generated_at=data.get('generated_at') if isinstance(
        data.get('generated_at'), str) else None, hosts=hosts)


def load_view(home=None):
    """Re-project the private file before embedding it in a public page."""
    try:
        return project_view(read(Path(home or Path.home()) / '.local/state/plangraph/agents-view.json'))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
