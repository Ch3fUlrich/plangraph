"""Round-one evidence schema only; routing behaviour belongs to later slices."""
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
