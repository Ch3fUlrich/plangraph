"""Cross-language reconciliation fixtures use the complete Python Leg shape."""
import json
from dataclasses import asdict
from pathlib import Path

from plangraph.planner.legs import Leg
from plangraph.spawner.leg_probe import key


def test_reconcile_leg_key_fixture():
    path = Path(__file__).resolve().parents[1] / 'rust/pg-spawnd/tests/fixtures/reconcile_legs.json'
    rows = json.loads(path.read_text())
    assert [(r['leg']['harness'], '/' in r['leg']['model']) for r in rows] == [
        ('agy', False), ('agy', True), ('opencode', False)]
    for row in rows:
        leg = Leg(**row['leg'])
        assert asdict(leg) == dict(row['leg'], expires=None)
        assert key(leg) == row['expected']
