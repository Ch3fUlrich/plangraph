"""Opt-in incident boundary; host ownership, sampling and leg stop are injected.

The identity callback must compare the recorded start token to a live process
(not infer ownership from a PID). The sampler supplies structured process/socket
evidence, never command lines, environments, prompt bodies or raw log text.
Cancellation must stop only this leg under its existing wall cap; run-level
cancel is deliberately not a default. No live acceptance is implied here.
"""
import json
import math
from pathlib import Path

from .private_io import atomic_write_private


class HungBoundary:
    def __init__(self, reader, *, sample=None, identity=None, cancel=None, fallthrough=None,
                 minutes=12, write=atomic_write_private):
        if type(minutes) not in (int, float) or not math.isfinite(minutes) or minutes <= 0:
            raise ValueError('requires finite positive minutes')
        self.reader, self.sample, self.identity = reader, sample or self._sample, identity or self._identity
        self.cancel, self.fallthrough, self.write = cancel or self._cancel, fallthrough or self._fallthrough, write
        self.seconds, self.waiting = minutes * 60, set()
        if not math.isfinite(self.seconds):
            raise ValueError('threshold overflow')

    @staticmethod
    def _identity(pid, ident):
        from .proc_kill import alive
        return ident is not None and alive(pid, ident)

    @staticmethod
    def _sample(record):
        from .proc_kill import snapshot, _stat
        tree = snapshot(record['pid'], record['root_identity'])
        if HungBoundary._identity(record['pid'], record['root_identity']):
            tree[record['pid']] = record['root_identity']
        return dict(tokens=record.get('tokens'), output_bytes=record.get('output_bytes'),
                    gateway_socket=record.get('gateway_socket'), queue_limit=record.get('queue_limit'),
                    tree=[dict(pid=p, parent=_stat(p)[1], start=t) for p, t in tree.items()])

    def _cancel(self, record):
        import os
        import signal
        if self._identity(record['pid'], record['root_identity']):
            os.kill(record['pid'], signal.SIGTERM)
        return not self._identity(record['pid'], record['root_identity'])

    def _fallthrough(self, record):
        self.waiting.discard(record.get('leg'))

    @property
    def enabled(self):
        return getattr(self.reader, 'enabled', False) is True

    def rank(self, legs):
        """Stable next-tick preference; caller's disabled ordering is untouched."""
        return sorted(legs, key=lambda leg: leg in self.waiting) if self.enabled else list(legs)

    def probe(self, state, record, now, *, dry_run=False):
        event = dict(rule='unknown', run_id=record.get('run_id'), leg=record.get('leg'))
        try:
            started = record.get('started')
            if (record.get('status') != 'running' or type(started) not in (int, float)
                    or not math.isfinite(started) or not math.isfinite(now)
                    or now - started < self.seconds):
                return event
            pid = record.get('pid')
            ident = record.get('pid_start', record.get('root_identity'))
            def owned():
                return type(pid) is int and pid > 1 and ident is not None and self.identity(pid, ident)
            if not owned():
                return dict(event, identity='unproven')
            evidence = self.sample(record)
            # Missing usage/output is unknown, never an implicit zero.
            if any(type(evidence.get(k)) not in (int, float) or evidence[k] != 0
                   for k in ('tokens', 'output_bytes')):
                self.waiting.discard(record.get('leg'))
                return event
            connected = evidence.get('gateway_socket')
            if connected is True:
                if evidence.get('queue_limit') is True:
                    if not dry_run:
                        self.waiting.add(record.get('leg'))
                    return dict(event, rule='queue_wait')
                return event
            if connected is not False or not owned():
                return event
            event['rule'] = 'leg_hung'
            if dry_run:
                return event
            run_id = record['run_id']
            if not isinstance(run_id, str) or Path(run_id).name != run_id or run_id in ('.', '..'):
                raise ValueError('invalid run id')
            # Only numeric tree identities and fixed log codes cross this boundary.
            tree = evidence['tree']
            if not isinstance(tree, list) or not tree or len(tree) > 128:
                raise ValueError('invalid bounded process tree')
            safe_tree = []
            for row in tree:
                safe = {k: row[k] for k in ('pid', 'parent', 'start')}
                if any(type(v) is not int or v < 0 for v in safe.values()):
                    raise ValueError('invalid process identity')
                safe_tree.append(safe)
            if not any(r['pid'] == pid and r['start'] == ident for r in safe_tree):
                raise ValueError('root absent from capture')
            payload = dict(tree=safe_tree, gateway_socket=False,
                           queue_limit=evidence.get('queue_limit') is True,
                           logs=[v for v in evidence.get('logs', [])[:32]
                                 if v in ('queue_limit', 'gateway_connected', 'gateway_absent')])
            destination = Path(state) / 'runs' / run_id / 'leg-hung.json'
            if destination.exists():
                raise FileExistsError('retain previous incident evidence')
            self.write(destination, json.dumps(payload))
            event['captured'] = True
            if not owned():
                return dict(event, identity='changed')
            event['cancelled'] = self.cancel(record) is True
            if event['cancelled']:
                self.fallthrough(record)
            return event
        except Exception as exc:
            # Do not echo sampler errors: they may contain private transport data.
            return dict(event, capture_error=type(exc).__name__)
