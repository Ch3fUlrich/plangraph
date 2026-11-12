"""One synthetic credential table shared with worker-core's Rust tests."""

import json
import re
from pathlib import Path

import pytest

from plangraph.spawner import env_redact
from plangraph.spawner.env_redact import redact
from plangraph.spawner.redact_tokens import SPECS


TABLE = json.loads(
    (Path(__file__).parent / "fixtures" / "redact_parity.json").read_text(encoding="utf-8")
)


def joined(value):
    return ''.join(value) if isinstance(value, list) else value


def input_text(row):
    return joined(row.get('input', row.get('input_parts')))


@pytest.mark.parametrize("row", [r for r in TABLE if "expected" in r])
def test_redact_parity(row):
    assert redact(input_text(row)) == joined(row['expected'])


@pytest.mark.parametrize("row", [r for r in TABLE if "source" in r])
def test_redact_source_parity(row):
    assert env_redact.redact_source(input_text(row)) == joined(row['source'])


@pytest.mark.parametrize('row', [r for r in TABLE if 'expected' in r])
def test_redact_output_parity(row):
    assert env_redact.redact_output(input_text(row)) == joined(row.get('output', row['expected']))
    if row.get('output_only'):
        assert joined(row['expected']) == input_text(row)
        assert env_redact.redact_source(input_text(row)) == input_text(row)


def test_catalog_is_exactly_rust_specs():
    rust = (Path(__file__).parents[1] / 'rust/worker-core/src/redact_tokens.rs').read_text()
    table = rust.split('pub(crate) const SPECS:', 1)[1].split('];', 1)[0]
    rows = re.findall(r'TokenSpec\s*\{\s*prefix:\s*"([^"]+)",\s*body:\s*"([^"]+)",'
                      r'\s*floor:\s*(\d+),\s*exact:\s*(true|false),\s*\}', table)
    assert rows and list(SPECS) == [(p, b, int(f), e == 'true') for p, b, f, e in rows]
