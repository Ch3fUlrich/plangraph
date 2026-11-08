"""Default spawn admission shared by CLI supervisors and MCP."""
import json
import os
import uuid
from pathlib import Path


def resources(service):
    live = sum(r.get('status') == 'running' and not r.get('overflow_remote')
               for r in service.ps()['runs'])
    memory = next(int(s.split()[1]) // 1024 for s in Path('/proc/meminfo').read_text().splitlines()
                  if s.startswith('MemAvailable:'))
    return live, memory


def admit(service, options):
    if options.get('dry_run'):
        return None
    from . import overflow
    path = Path(os.environ.get('PLANGRAPH_OVERFLOW_HOSTS',
                              '~/.config/plangraph/overflow-hosts.json')).expanduser()
    configured = path.exists()
    try:
        config = json.loads(path.read_text()) if configured else {}
        if not isinstance(config, dict):
            config = {}
    except (OSError, ValueError):
        config = {}
    def limit(key, default):
        try:
            return max(0, int(config.get(key, default)))
        except (ValueError, TypeError, OverflowError):
            return default
    live, memory = resources(service)
    cap, floor = limit('local_run_cap', 4), limit('local_floor_mb', 3000)
    if live < cap and memory >= floor:
        return None
    if not configured:
        service._overflow_reason = 'overflow unconfigured'
        return None
    card = options.get('card') or {}
    repo = Path((card.get('diff_ref') or {}).get('repo') or
                getattr(service.runner, 'default_cwd', None) or os.getcwd()).resolve()
    public = str(repo) in [str(Path(p).expanduser().resolve()) for p in config.get('public_repos', [])]
    rows = overflow.ready_records(config)
    flags = []
    for key in ('role', 'model', 'leg', 'override_reason', 'review_of', 'review_scope'):
        if options.get(key) is not None:
            flags.extend(('--' + key.replace('_', '-'), str(options[key])))
    for family in options.get('writer_families', ()):
        flags.extend(('--writer-family', family))
    for key in ('seats', 'no_fallthrough', 'require_bench'):
        if options.get(key):
            flags.append('--' + key.replace('_', '-'))
    while True:
        decision = overflow.decide(live, memory, cap, floor, rows, public)
        if decision.host is None:
            record = {'run_id': 'run-' + uuid.uuid4().hex, 'status': 'queued',
                          'reason': decision.reason, 'overflow_reason': decision.reason, 'exit_code': 5}
            break
        name = decision.host
        try:
            if options.get('benchmark') is not None:
                raise ValueError('local benchmark database cannot be forwarded')
            result = overflow.remote_spawn(name, config['hosts'][name], card,
                                           options.get('prompt', ''), flags)
        except (OSError, ValueError, KeyError, TypeError):
            result = None
        if result is not None:
            record = dict(result, host=name, overflow_reason=decision.reason, overflow_remote=True)
            record.setdefault('run_id', 'run-' + uuid.uuid4().hex)
            break
        rows[name]['refusal'] = 'remote spawn failed'
    service._save(record)
    return record
