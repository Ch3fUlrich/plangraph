"""Console credit routing contracts (D-3258)."""
from datetime import date

import pytest

from plangraph.planner.legs import DEFAULT_LEGS, TIERS, WRITER_ORDER
from plangraph.planner.route_policy import RoutePolicy
from plangraph.planner.routing import validate_model


def test_console_legs():
    assert TIERS == ('credit', 'trial', 'free', 'subscription', 'paid')
    assert WRITER_ORDER[0] == 'console-sonnet-5.5'
    for name in ('sonnet', 'opus', 'haiku'):
        model = f'console-anthropic/claude-{name}-5-5'
        assert validate_model(model) == model
        leg = next(l for l in DEFAULT_LEGS if l.model == model)
        assert leg.tier == 'credit' and leg.family == 'claude'


def test_credit_expiry():
    leg = next(l for l in DEFAULT_LEGS if l.id == 'console-sonnet-5.5')
    policy = RoutePolicy(credit_tiers={'console-anthropic': {'expires': '2026-11-30'}},
                         today=date(2026, 12, 1))
    assert policy.skip_reason(leg, 'implement') == 'credit_expired'
    assert RoutePolicy().skip_reason(leg, 'implement') == 'credit_expired'


@pytest.mark.parametrize('entry', [{}, {'expires': 'nov'}, {'expires': None}])
def test_in_process_invalid_credit_expiry(entry):
    leg = next(l for l in DEFAULT_LEGS if l.tier == 'credit')
    assert RoutePolicy(credit_tiers={'console-anthropic': entry}).skip_reason(leg, 'implement') == 'credit_expired'


def test_credit_last_usable_day():
    leg = next(l for l in DEFAULT_LEGS if l.tier == 'credit')
    policy = RoutePolicy(credit_tiers={'console-anthropic': {'expires': '2026-11-30'}}, today=date(2026, 11, 30))
    assert policy.skip_reason(leg, 'implement') is None


def test_spend_store_and_missing(tmp_path):
    import json
    import sqlite3
    from types import SimpleNamespace
    from plangraph.spawner.credit import record_spend, credit_report
    db = tmp_path / 'opencode.db'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE session_v2 (id TEXT, title TEXT, time_created INTEGER)')
        conn.execute('CREATE TABLE session_message (session_id TEXT, type TEXT, data TEXT)')
        conn.execute("INSERT INTO session_v2 VALUES ('s', 'plangraph-j', 1000)")
        conn.execute('INSERT INTO session_message VALUES (?, ?, ?)',
                     ('s', 'assistant', json.dumps({'cost': 1.25, 'tokens': {'input': 20, 'output': 3}})))
    leg = next(l for l in DEFAULT_LEGS if l.tier == 'credit')
    job = SimpleNamespace(job_id='j', status='failed')
    record_spend(tmp_path, {'run_id': 'r', 'started': 1}, leg, job, db_path=db)
    record_spend(tmp_path, {'run_id': 'r2'}, leg, job, db_path=tmp_path / 'missing')
    lines = [json.loads(s) for s in (tmp_path / 'credit-spend.jsonl').read_text().splitlines()]
    assert lines[0]['cost_usd'] == 1.25
    assert lines[1]['cost_usd'] is None and lines[1]['source'] == 'unavailable'
    policy = RoutePolicy(credit_tiers={'console-anthropic': {'expires': '2026-11-30'}})
    assert credit_report(tmp_path, policy=policy)[0]['tokens_input'] == 20


def test_credits_cli(tmp_path, capsys, monkeypatch):
    import json
    from plangraph.spawner.cli import main
    monkeypatch.delenv('PLANGRAPH_ROUTE_POLICY', raising=False)
    assert main(['--state', str(tmp_path), 'credits', '--json']) == 0
    assert json.loads(capsys.readouterr().out)[0]['provider'] == 'console-anthropic'


def test_routing_expiry_and_seats():
    from plangraph.planner.routing import plan_spawn
    config = {'console-anthropic': {'expires': '2026-11-30'}}
    policy = RoutePolicy(credit_tiers=config, today=date(2026, 11, 1))
    reader = lambda *a, **k: {'state': 'ok', 'evidence': {}}
    args = dict(leg_reader=reader, policy=policy)
    assert plan_spawn({'estimate_tokens': 100}, role='implement', **args).first_leg.id == 'console-sonnet-5.5'
    review = plan_spawn({'estimate_tokens': 100}, role='review', writer_families=('gpt',), **args)
    assert all(l.tier == 'credit' for l in review.legs[:3])
    review = plan_spawn({'estimate_tokens': 100}, role='review', writer_families=('claude',), **args)
    assert all(l.family != 'claude' for l in review.review_seats)
    args['policy'] = RoutePolicy(credit_tiers=config, today=date(2026, 12, 1))
    plan = plan_spawn({'estimate_tokens': 100}, role='implement', **args)
    assert plan.first_leg.id == 'agy-gemini-3.8-flash'
    assert {'leg': 'console-sonnet-5.5', 'reason': 'credit_expired', 'skip_reason': 'credit_expired', 'evidence': {}} in plan.skipped


@pytest.mark.parametrize('entry', [{'expires': 'bad'}, {'expires': '2026-11-30', 'extra': 1}, {'note': 'missing date'}])
def test_policy_validation(tmp_path, entry):
    import json
    from plangraph.planner.route_policy import load_route_policy
    path = tmp_path / 'policy.json'
    path.write_text(json.dumps({'credit_tiers': {'console-anthropic': entry}}))
    with pytest.raises(ValueError, match='credit_tiers'):
        load_route_policy(path)


def test_console_not_served_on_linux():
    from plangraph.routing_support.resolver import host_skip_reason
    reason = host_skip_reason({'model': 'console-anthropic/claude-sonnet-5-5', 'harness': 'opencode'},
                              probe=lambda env: set())
    assert reason.startswith('not_served_on_host')


def test_console_probe_unavailable_is_not_admitted():
    from plangraph.routing_support.resolver import host_skip_reason
    assert host_skip_reason({'model': 'console-anthropic/claude-sonnet-5-5', 'harness': 'opencode'},
                            probe=lambda env: None).startswith('not_served_on_host')


def test_credit_expiry_boundary_pinned():
    leg = next(l for l in DEFAULT_LEGS if l.tier == 'credit')
    config = {'console-anthropic': {'expires': '2026-11-30'}}
    verdicts = ((date(2026, 11, 29), None), (date(2026, 11, 30), None),
                (date(2026, 12, 1), 'credit_expired'))
    for day, expect in verdicts:
        assert RoutePolicy(credit_tiers=config, today=day).skip_reason(leg, 'implement') == expect


def test_credit_report_uses_policy_today(tmp_path):
    from plangraph.spawner.credit import credit_report
    policy = RoutePolicy(credit_tiers={'console-anthropic': {'expires': '2026-11-30'}},
                         today=date(2026, 11, 29))
    assert credit_report(tmp_path, policy=policy)[0]['days_left'] == 1


def test_expiry_documented_as_last_usable_day():
    import json
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    policy = json.loads((root / 'src/plangraph/planner/route_policy.json').read_text(encoding='utf-8'))
    entry = policy['credit_tiers']['console-anthropic']
    assert 'last usable day' in entry.get('note', '').lower()
    docs = (root / 'docs/SPAWNER.md').read_text(encoding='utf-8')
    assert 'last usable day' in docs.lower()


def test_credit_tier_is_own_top_level_section():
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    docs = (root / 'docs/SPAWNER.md').read_text(encoding='utf-8')
    assert re.search(r'^# Credit tier \(D-3258\)$', docs, re.M)
    assert 'Install the package on each host, then use' in docs
    assert '`--state PATH` precedes the verb; otherwise' in docs


def test_credits_human_table_by_default(tmp_path, capsys, monkeypatch):
    import json
    from plangraph.spawner.cli import main
    monkeypatch.delenv('PLANGRAPH_ROUTE_POLICY', raising=False)
    assert main(['--state', str(tmp_path), 'credits']) == 0
    out = capsys.readouterr().out
    with pytest.raises(ValueError):
        json.loads(out)
    for word in ('provider', 'expires', 'days_left', 'console-anthropic'):
        assert word in out
