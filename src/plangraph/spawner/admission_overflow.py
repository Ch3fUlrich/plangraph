"""Shared CLI/MCP admission with allowlisted SSH overflow."""
import json
import os
import uuid
from pathlib import Path


def resources(service):
    from .overflow import available_memory_mb
    return 0, available_memory_mb()


def admit(service, options, *, card=None):
    if options.get('dry_run'):
        return None
    from . import overflow
    path = Path(os.environ.get('PLANGRAPH_OVERFLOW_HOSTS',
                              '~/.config/plangraph/overflow-hosts.json')).expanduser()
    if not path.exists():
        return None
    try:
        config = json.loads(path.read_text())
        if not isinstance(config, dict):
            config = {}
    except (OSError, ValueError):
        config = {}
    role = options.get('role', 'implement')
    role_mb, floor = overflow.local_limits(overflow.registry_caps(), role)
    live, memory = resources(service)
    if not overflow.memory_refusal(memory, role_mb, floor):
        return None
    repo = Path(((card or {}).get('diff_ref') or {}).get('repo') or
                getattr(service.runner, 'default_cwd', None) or os.getcwd()).resolve()
    public = str(repo) in [str(Path(p).expanduser().resolve()) for p in config.get('public_repos', [])]
    rows = overflow.ready_records(config, role)
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
        decision = overflow.decide(live, memory, role_mb, floor, rows, public)
        if options.get('benchmark') is not None and decision.host is not None:
            decision = overflow.Decision(None, 'local benchmark database cannot be forwarded')
        if decision.host is None:
            record = {'run_id': 'run-' + uuid.uuid4().hex, 'status': 'queued',
                      'reason': decision.reason, 'overflow_reason': decision.reason, 'exit_code': 5}
            break
        name = decision.host
        try:
            result = overflow.remote_spawn(name, config['hosts'][name], card or {},
                                           options.get('prompt', ''), flags)
            refusal = 'remote spawn failed'
        except overflow.RemoteRefusal as exc:
            result, refusal = None, str(exc)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            result, refusal = None, overflow.failure_reason(exc)
        if result is not None:
            record = dict(result, host=name, overflow_reason=decision.reason, overflow_remote=True)
            record.setdefault('status', 'unknown')
            break
        rows[name]['refusal'] = refusal
    service._save(record)
    return record
