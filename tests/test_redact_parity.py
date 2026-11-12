"""One synthetic credential table shared with worker-core's Rust tests."""

import json
from pathlib import Path

import pytest

from plangraph.spawner import env_redact
from plangraph.spawner.env_redact import redact


TABLE = json.loads(
    (Path(__file__).parent / "fixtures" / "redact_parity.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize("row", [r for r in TABLE if "expected" in r])
def test_redact_parity(row):
    text = row["input"] if "input" in row else "".join(row["input_parts"])
    assert redact(text) == row["expected"]


@pytest.mark.parametrize("row", [r for r in TABLE if "source" in r])
def test_redact_source_parity(row):
    assert env_redact.redact_source(''.join(row['input_parts'])) == row['source']
