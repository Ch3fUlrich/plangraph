"""PG-only, strictly numeric host telemetry and ingest-only identities."""
import json
import math
import os
import re
import secrets
from dataclasses import dataclass

from . import auth
from .errors import Refused

TABLES = ('host_samples_raw', 'host_samples_1m', 'host_samples_1h', 'host_sample_tokens')
PREFIX = 'pgh1_'
METRICS = ('cpu_pct', 'mem_available_bytes', 'swap_in_bytes_s', 'disk_free_bytes')
GPU_METRICS = ('util_pct', 'mem_free_bytes', 'mem_total_bytes')


@dataclass(frozen=True)
class HostPrincipal(auth.Principal):
    token_id: int = 0
    host_id: str = ''


def authenticate_host(conn, token):
    if not hasattr(conn, 'schema'):
        raise Refused('forbidden', 'unknown host token')
    row = conn.execute('SELECT id,host_id FROM host_sample_tokens '
                       'WHERE token_sha256=? AND revoked_at IS NULL',
                       (auth.hash_token(token),)).fetchone()
    if row is None:
        raise Refused('forbidden', 'unknown or revoked host token')
    return HostPrincipal('host:' + row['host_id'], 'host-ingest', [], None,
                         row['id'], row['host_id'])


def invalid():
    raise Refused('validation', 'invalid host sample schema')


def number(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        invalid()


def host_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value):
        invalid()


def pair(value, percent=False):
    if type(value) is not dict or set(value) != {'mean', 'max'}:
        invalid()
    for n in value.values():
        number(n)
    if value['mean'] > value['max'] or (percent and value['max'] > 100):
        invalid()


def validate(body):
    required = {'host_id', 'seq', 'agent_started_at', 'gpus', 'agent_processes', *METRICS}
    if type(body) is not dict or set(body) - {'rule_overrides'} != required:
        invalid()
    host_id(body['host_id'])
    if type(body['seq']) is not int or not 0 <= body['seq'] < 2**63:
        invalid()
    number(body['agent_started_at'])
    for key in METRICS:
        pair(body[key], key == 'cpu_pct')
    if type(body['gpus']) is not list or len(body['gpus']) > 64:
        invalid()
    indices = set()
    for gpu in body['gpus']:
        if type(gpu) is not dict or set(gpu) != {'index', *GPU_METRICS}:
            invalid()
        idx = gpu['index']
        if type(idx) is not int or idx < 0 or idx in indices:
            invalid()
        indices.add(idx)
        for key in GPU_METRICS:
            if gpu[key] is not None:
                pair(gpu[key], key == 'util_pct')
    counts = body['agent_processes']
    if type(counts) is not dict or set(counts) != {'claude', 'opencode', 'qodercli', 'agy'}:
        invalid()
    if any(type(n) is not int or not 0 <= n < 2**31 for n in counts.values()):
        invalid()
    # Override reports are numeric values for known rule keys, never free text.
    overrides = body.get('rule_overrides', {})
    if type(overrides) is not dict or set(overrides) - {
            'HostFresh', 'HostWorkerCap', 'HostMemoryFloor'}:
        invalid()
    for value in overrides.values():
        number(value)


def http_read(store, who, name, query):
    if name == 'host_samples':
        return store.host_samples(who)
    return store.host_samples_range(who, query.get('host_id', [''])[0],
                                   query.get('from', [0])[0], query.get('to', [0])[0],
                                   query.get('resolution', ['raw'])[0])


def http_write(store, who, name, body):
    if name == 'ingest_host_sample':
        return store.ingest_host_sample(who, body)
    if type(body) is not dict:
        invalid()
    if name == 'mint_host_token':
        return store.mint_host_token(who, body.get('host_id'))
    return store.revoke_host_token(who, body.get('token_id'))


class HostSamplesMixin:
    def _host_pg(self):
        if not hasattr(self.conn, 'schema'):
            raise Refused('unavailable', 'host samples require Postgres')

    def _host_reader(self, principal, admin=False):
        self._host_pg()
        if type(principal) is not auth.Principal or principal.role == 'system':
            raise Refused('forbidden', 'normal keeper principal required')
        self._check_principal(principal)
        if admin:
            self._require_role(principal, 'router', 'l1')

    def mint_host_token(self, principal, host_id):
        self._host_reader(principal, True)
        globals()['host_id'](host_id)
        token = PREFIX + secrets.token_urlsafe(32)
        with self._write():
            self._host_reader(principal, True)
            ident = self.conn.execute('SELECT COALESCE(MAX(id),0)+1 FROM host_sample_tokens').fetchone()[0]
            self.conn.execute('INSERT INTO host_sample_tokens(id,host_id,token_sha256) VALUES(?,?,?)',
                              (ident, host_id, auth.hash_token(token)))
        return dict(id=ident, host_id=host_id, token=token)

    def revoke_host_token(self, principal, token_id):
        """Refuse unknown ids; revoking an already-revoked id is idempotent."""
        self._host_reader(principal, True)
        with self._write():
            self._host_reader(principal, True)
            row = self.conn.execute('UPDATE host_sample_tokens '
                                    'SET revoked_at=COALESCE(revoked_at,?) WHERE id=? RETURNING id',
                                    (self.clock(), token_id)).fetchone()
            if row is None:
                raise Refused('not_found', 'unknown host token id')
        return dict(id=token_id)

    def ingest_host_sample(self, principal, body):
        self._host_pg()
        if type(principal) is not HostPrincipal:
            raise Refused('forbidden', 'host ingest token required')
        if type(body) is not dict or not body:
            invalid()
        with self._write():
            row = self.conn.execute('SELECT host_id FROM host_sample_tokens WHERE id=? '
                                    'AND revoked_at IS NULL', (principal.token_id,)).fetchone()
            if row is None or row[0] != principal.host_id or body.get('host_id') != row[0]:
                raise Refused('forbidden', 'host token scope mismatch or revoked')
            validate(body)
            now = self.clock()
            self.conn.execute('SELECT host_samples_partitions(?)', (now,))
            previous = self.conn.execute('SELECT sample FROM host_samples_raw WHERE host_id=? '
                                         'ORDER BY received_at DESC, seq DESC LIMIT 1', (row[0],)).fetchone()
            old = json.loads(previous[0]) if previous else None
            restarted = bool(old and old['agent_started_at'] != body['agent_started_at'])
            gap = max(0, body['seq'] - old['seq'] - 1) if old and not restarted else 0
            self.conn.execute('INSERT INTO host_samples_raw '
                              '(host_id,received_at,seq,sample,seq_gap,restarted) VALUES(?,?,?,?,?,?)',
                              (row[0], now, body['seq'], json.dumps(body), gap, int(restarted)))
        return dict(received_at=now)

    @staticmethod
    def _host_row(row):
        return dict(received_at=row['received_at'], sample=json.loads(row['sample']),
                    seq_gap=row['seq_gap'], restarted=bool(row['restarted']),
                    sample_count=row['sample_count'])

    def host_samples(self, principal):
        self._host_reader(principal)
        rows = self.conn.execute('SELECT DISTINCT ON (host_id) * FROM host_samples_raw '
                                 'ORDER BY host_id,received_at DESC,seq DESC').fetchall()
        interval = float(os.environ.get('PG_HOST_SAMPLE_S', '60'))
        if not math.isfinite(interval) or interval <= 0:
            interval = 60
        result = []
        for row in rows:
            out = self._host_row(row)
            age = self.clock() - row['received_at']
            out.update(host_id=row['host_id'], state='down' if age >= 10 * interval else
                       'stale' if age >= 3 * interval else 'fresh')
            result.append(out)
        return result

    def host_samples_range(self, principal, host_id, start, end, resolution='raw'):
        self._host_reader(principal)
        globals()['host_id'](host_id)
        if resolution not in ('raw', '1m', '1h'):
            invalid()
        try:
            start, end = float(start), float(end)
        except (ValueError, TypeError, OverflowError):
            invalid()
        number(start)
        number(end)
        if end < start:
            invalid()
        cap = {'raw': 86400, '1m': 30 * 86400}.get(resolution)
        if cap is not None and end - start > cap:
            invalid()
        rows = self.conn.execute(f'SELECT * FROM host_samples_{resolution} WHERE host_id=? '
                                 'AND received_at>=? AND received_at<? ORDER BY received_at,seq LIMIT 10000',
                                 (host_id, start, end)).fetchall()
        result = [self._host_row(row) for row in rows]
        return dict(rows=result, truncated=True) if len(result) == 10000 else result

    def maintain_host_samples(self):
        self._host_pg()
        from .host_samples_rollup import maintain
        with self._write():
            maintain(self.conn, self.clock())
