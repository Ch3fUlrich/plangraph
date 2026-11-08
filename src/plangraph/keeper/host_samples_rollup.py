"""Owner-installed partitions and idempotent, keeper-owned telemetry rollups."""
import copy
import json

from .store_host_samples import METRICS, GPU_METRICS

DDL = """
CREATE TABLE host_sample_tokens (
    id BIGINT PRIMARY KEY,
    host_id TEXT COLLATE "C" NOT NULL,
    token_sha256 TEXT COLLATE "C" NOT NULL UNIQUE,
    revoked_at DOUBLE PRECISION
);
CREATE TABLE host_samples_raw (
    host_id TEXT COLLATE "C" NOT NULL,
    received_at DOUBLE PRECISION NOT NULL,
    seq BIGINT NOT NULL,
    sample TEXT COLLATE "C" NOT NULL,
    seq_gap BIGINT NOT NULL DEFAULT 0,
    restarted INTEGER NOT NULL DEFAULT 0,
    sample_count BIGINT NOT NULL DEFAULT 1,
    PRIMARY KEY (host_id, received_at, seq)
) PARTITION BY RANGE (received_at);
CREATE TABLE host_samples_1m (
    host_id TEXT COLLATE "C" NOT NULL,
    received_at DOUBLE PRECISION NOT NULL,
    seq BIGINT NOT NULL,
    sample TEXT COLLATE "C" NOT NULL,
    seq_gap BIGINT NOT NULL DEFAULT 0,
    restarted INTEGER NOT NULL DEFAULT 0,
    sample_count BIGINT NOT NULL,
    PRIMARY KEY (host_id, received_at)
);
CREATE TABLE host_samples_1h (
    host_id TEXT COLLATE "C" NOT NULL,
    received_at DOUBLE PRECISION NOT NULL,
    seq BIGINT NOT NULL,
    sample TEXT COLLATE "C" NOT NULL,
    seq_gap BIGINT NOT NULL DEFAULT 0,
    restarted INTEGER NOT NULL DEFAULT 0,
    sample_count BIGINT NOT NULL,
    PRIMARY KEY (host_id, received_at)
);
CREATE FUNCTION host_samples_partitions(moment DOUBLE PRECISION) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path FROM CURRENT AS $$
DECLARE day BIGINT; base BIGINT; name TEXT;
BEGIN
  IF moment < 0 OR moment > 100000000000 THEN
    RAISE EXCEPTION 'invalid partition time';
  END IF;
  PERFORM pg_advisory_xact_lock(736291);
  base := floor(moment / 86400)::bigint;
  FOR day IN base..base+2 LOOP
    name := 'host_samples_day_' || day;
    EXECUTE format('CREATE TABLE IF NOT EXISTS %I PARTITION OF host_samples_raw '
                   'FOR VALUES FROM (%s) TO (%s)', name, day*86400, (day+1)*86400);
  END LOOP;
END;
$$;
"""


def combine(entries):
    body = copy.deepcopy(entries[-1][0])
    total = sum(n for _, n in entries)

    def aggregate(values):
        values = [(v, n) for v, n in values if v is not None]
        if not values:
            return None
        return dict(mean=sum(v['mean'] * n for v, n in values) / sum(n for _, n in values),
                    max=max(v['max'] for v, _ in values))

    for key in METRICS:
        body[key] = aggregate([(b[key], n) for b, n in entries])
    body['gpus'] = []
    indices = sorted({g['index'] for b, _ in entries for g in b['gpus']})
    for idx in indices:
        gpu = dict(index=idx)
        for key in GPU_METRICS:
            gpu[key] = aggregate([(g[key], n) for b, n in entries
                                   for g in b['gpus'] if g['index'] == idx])
        body['gpus'].append(gpu)
    return body, total


def rollup(conn, source, target, width, now):
    groups = {}
    latest = conn.execute(f'SELECT MAX(received_at) FROM host_samples_{target}').fetchone()[0]
    lower = max(0, latest - width) if latest is not None else 0
    for row in conn.execute(f'SELECT * FROM host_samples_{source} '
                            'WHERE received_at>=? AND received_at<? ORDER BY received_at,seq',
                            (lower, now // width * width)).fetchall():
        key = (row['host_id'], row['received_at'] // width * width)
        groups.setdefault(key, []).append(row)
    for (host, bucket), rows in groups.items():
        body, count = combine([(json.loads(r['sample']), r['sample_count']) for r in rows])
        # Do not replace a complete retained bucket with a partial source bucket
        # after source retention has removed its earliest rows.
        conn.execute(f'INSERT INTO host_samples_{target} '
                     '(host_id,received_at,seq,sample,seq_gap,restarted,sample_count) '
                     'VALUES(?,?,?,?,?,?,?) ON CONFLICT(host_id,received_at) DO UPDATE SET '
                     'seq=excluded.seq,sample=excluded.sample,seq_gap=excluded.seq_gap,'
                     'restarted=excluded.restarted,sample_count=excluded.sample_count '
                     f'WHERE excluded.sample_count>=host_samples_{target}.sample_count',
                     (host, bucket, body['seq'], json.dumps(body),
                      sum(r['seq_gap'] for r in rows),
                      int(any(r['restarted'] for r in rows)), count))


def maintain(conn, now):
    conn.execute('SELECT host_samples_partitions(?)', (now,))
    rollup(conn, 'raw', '1m', 60, now)
    rollup(conn, '1m', '1h', 3600, now)
    for tier, days in [('raw', 14), ('1m', 90), ('1h', 730)]:
        conn.execute(f'DELETE FROM host_samples_{tier} WHERE received_at<?',
                     (now - days * 86400,))
