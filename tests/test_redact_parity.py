"""One synthetic credential table shared with worker-core's Rust tests."""

import json
from pathlib import Path

import pytest

from plangraph.spawner.runner import redact


TABLE = json.loads(
    (Path(__file__).parent / "fixtures" / "redact_parity.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize("row", TABLE, ids=lambda row: row["input"])
def test_redact_parity(row):
    actual = redact(row["input"])
    assert actual == row["expected"]
    assert redact(actual) == actual
