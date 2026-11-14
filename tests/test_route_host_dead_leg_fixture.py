"""Recorded evidence only; routing behaviour belongs to later slices."""
import json
from pathlib import Path
import re


def test_route_host_dead_leg_attempts_fixture_shape():
    path = Path(__file__).parent / 'fixtures' / 'route_host_dead_leg_attempts.json'
    runs = json.loads(path.read_text(encoding='utf-8'))
    assert isinstance(runs, dict)
    assert set(runs) == {
        'run-82a09c656e754444a420415ea53b4ba8',
        'run-df470d3124974db08a3c23302edb011a',
        'run-9f635c369cc3439c9986aa03e0a64181',
        'run-2a2b72736e0d48de904f10f03f31be45',
        'run-caedcb1b7a484edca2c2cebe20348105',
    }
    for rows in runs.values():
        assert isinstance(rows, list) and len(rows) == 26
        for row in rows:
            assert isinstance(row, dict)
            assert set(row) == {'leg', 'status', 'skip_reason', 'reason'}
            assert isinstance(row['leg'], str)
            assert re.fullmatch(r'[a-z0-9][a-z0-9.-]*', row['leg'])
            assert row['status'] in {'skipped', 'failed', 'completed'}
            for field in ('skip_reason', 'reason'):
                assert row[field] is None or (
                    isinstance(row[field], str) and 0 < len(row[field]) <= 300)


def test_route_host_dead_leg_recorded_failure_fields():
    path = Path(__file__).parent / 'fixtures' / 'route_host_dead_leg_attempts.json'
    runs = json.loads(path.read_text(encoding='utf-8'))
    for run, rows in runs.items():
        by_leg = {row['leg']: row for row in rows}
        assert len(by_leg) == 26
        assert by_leg['mimo-v2.6-flash'] == {
            'leg': 'mimo-v2.6-flash', 'status': 'failed',
            'skip_reason': 'harness exit code 1', 'reason': None,
        }
        native = by_leg['agy-claude-opus']
        assert native['status'] == 'failed'
        assert native['skip_reason'].startswith('error: invalid model selection')
        assert native['reason'] is None  # Absent in the record, not a canonical reason.
        argon = by_leg['opencode-gemini-4-argon']
        limited = run == 'run-caedcb1b7a484edca2c2cebe20348105'
        assert argon['status'] == ('skipped' if limited else 'failed')
        assert argon['skip_reason'] == (
            'limited' if limited else 'not_served_on_host: opencode/gemini-4-argon')
        assert by_leg['claude-haiku-5.5']['status'] == 'completed'
        for leg in ('agy-gemini-3.8-flash', 'agy-gemini-4-argon', 'agy-sonnet-5.5'):
            assert by_leg[leg]['status'] == 'skipped'
            assert by_leg[leg]['skip_reason'] == 'leg_state_snapshot_error'


def test_route_host_dead_leg_quota_fixture_records_429():
    path = Path(__file__).parent / 'fixtures' / 'route_host_dead_leg_quota.json'
    runs = json.loads(path.read_text(encoding='utf-8'))
    resets = {
        'run-29c2b8e5c20c474b921d475182d22464': '145h52m0s',
        'run-726ed4a8c28b4aa888d64bddf8c73c46': '145h52m13s',
    }
    assert set(runs) == set(resets)
    for run, rows in runs.items():
        assert isinstance(rows, list) and len(rows) == 1
        row = rows[0]
        assert set(row) == {'leg', 'status', 'skip_reason', 'reason', 'reset_at'}
        assert row['leg'] == 'agy-sonnet-5.5'
        assert row['status'] == 'failed'
        assert row['reset_at'] is None  # No absolute reset was recorded.
        message = ('Individual quota reached. Please upgrade your subscription '
                   f'to increase your limits. Resets in {resets[run]}.')
        assert row['skip_reason'] == 'harness_limit: error: ' + message
        assert row['reason'] == 'RESOURCE_EXHAUSTED (code 429): ' + message
        assert len(row['reason']) <= 300
