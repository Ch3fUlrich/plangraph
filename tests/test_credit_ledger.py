import json
import sqlite3
from types import SimpleNamespace

from plangraph.planner.legs import DEFAULT_LEGS
from plangraph.planner.route_policy import RoutePolicy
from plangraph.spawner import credit


def test_selection_and_bad_rows(tmp_path):
    db = tmp_path / 'db'
    with sqlite3.connect(db) as conn:
        conn.executescript('CREATE TABLE session_v2(id TEXT, title TEXT, time_created INTEGER);'
                           'CREATE TABLE session_message(session_id TEXT, type TEXT, data TEXT);')
        conn.executemany('INSERT INTO session_v2 VALUES (?, ?, ?)',
                         [('old', 'plangraph-j', 1000), ('new', 'plangraph-j', 3000)])
        for sid, raw in [('old', '{"cost":99}'), ('new', 'bad'),
                         ('new', '{"tokens":null,"cost":2}'),
                         ('new', '{"cost":3,"tokens":{"input":4}}')]:
            conn.execute('INSERT INTO session_message VALUES (?, ?, ?)', (sid, 'assistant', raw))
    result = credit.session_spend(db, 'j', started=2)
    assert result['cost_usd'] == 5
    assert result['tokens_input'] == 4
    assert result['reason']


def test_dedupe_and_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(credit.time, 'sleep', lambda _: None)
    leg = next(l for l in DEFAULT_LEGS if l.tier == 'credit')
    job = SimpleNamespace(job_id='j', status='failed')
    for _ in range(2):
        credit.record_spend(tmp_path, {'run_id': 'r'}, leg, job, db_path=tmp_path / 'missing')
    path = tmp_path / 'credit-spend.jsonl'
    assert len(path.read_text().splitlines()) == 1
    with path.open('a') as stream:
        stream.write(path.read_text() + 'broken\n')
    policy = RoutePolicy(credit_tiers={'console-anthropic': {'expires': '2026-11-30'}})
    row = credit.credit_report(tmp_path, policy=policy)[0]
    assert row['runs'] == row['unavailable_runs'] == row['unknown_runs'] == 1
    assert row['malformed_lines'] == 1


def test_platform_paths(monkeypatch):
    monkeypatch.setattr(credit.sys, 'platform', 'win32')
    assert str(credit.opencode_db_path({'LOCALAPPDATA': '/local'})) == '/local/opencode/opencode.db'
    monkeypatch.setattr(credit.sys, 'platform', 'darwin')
    assert str(credit.opencode_db_path({'HOME': '/Users/user'})) == '/Users/user/Library/Application Support/opencode/opencode.db'


def test_retry_and_diagnostic(monkeypatch):
    calls, sleeps = [], []
    def fail(*args, **kwargs):
        calls.append(args)
        raise sqlite3.OperationalError('WAL recovery needed')
    monkeypatch.setattr(credit.sqlite3, 'connect', fail)
    monkeypatch.setattr(credit.time, 'sleep', sleeps.append)
    result = credit.session_spend('/missing', 'j')
    assert len(calls) == 2 and sleeps == [1]
    assert result['source'] == 'unavailable' and 'WAL recovery' in result['reason']


def test_concurrent_writers_and_chosen_override(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from dataclasses import replace
    leg = replace(DEFAULT_LEGS[0], tier='credit', model='console-anthropic/custom')
    def write(i):
        job = SimpleNamespace(job_id=f'j{i}', status='completed',
                              credit_spend=dict(cost_usd=1, tokens_input=2, tokens_output=3, source='opencode'))
        credit.record_spend(tmp_path, {'run_id': f'r{i}'}, leg, job)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, list(range(12)) * 2))
    entries = [json.loads(line) for line in (tmp_path / 'credit-spend.jsonl').read_text().splitlines()]
    assert len(entries) == 12
    assert all(e['cost_usd'] == 1 and e['model'] == leg.model for e in entries)
