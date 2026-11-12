"""One synthetic credential table shared with worker-core's Rust tests."""

import json
import re
from pathlib import Path

import pytest

from plangraph.spawner import env_redact
from plangraph.spawner.env_redact import redact
from plangraph.spawner.redact_tokens import JWT, SPECS, TOKENS, redact_wrapped


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


def test_prefixed_line_before_my_secret_retains_main_assignment_scope():
    value = 'hunter' + '2value'
    standalone = 'MY_' + 'SECRET: ' + value
    # Main's Python assignment regex does not match MY_SECRET, even standalone.
    assert redact(standalone) == standalone
    for nl in ('\n', '\r\n'):
        text = 'x ' + 'gh' + 'p_' + 'A9' * 7 + nl + standalone
        assert redact(text) == 'x [REDACTED]: ' + value


def test_plain_order_never_reveals_pieces_masked_by_old_order():
    def old_order(text):
        # Main's assignment-then-wrapped order; these inputs need no PEM/Bearer/URL rules.
        text = env_redact._redact_assignments(text)
        return JWT.sub('[REDACTED]', TOKENS.sub('[REDACTED]', redact_wrapped(text)))

    first, rest, value = 'A9' * 7, 'B8' * 8, 'hunter' + '2value'
    short = 'gh' + 'p_' + first
    full = short + 'C7' * 3
    inputs = []
    for nl in ('\n', '\r\n'):
        inputs.extend([
            'x ' + short + nl + 'API_KEY=' + value,
            'x ' + short + nl + 'PASSWORD=' + value,
            'x ' + short + nl + 'MY_SECRET: ' + value,
            'API_KEY=' + short + nl + rest + nl + 'next',
            'API_KEY="' + short + nl + rest + '"' + nl + 'next retained',
            "API_KEY='" + short + nl + rest + "'" + nl + 'next retained',
            'API_KEY=' + short + nl + 'PASSWORD=' + value,
            'API_KEY=' + short + nl + 'PASSWORD=[REDACTED]',
            'API_KEY=abc' + nl + 'next line of prose',
            'PASSWORD=' + rest + nl + 'next line of prose',
            full + nl + 'API_KEY=' + value,
            full + nl + 'API_KEY=[REDACTED]',
            'API_KEY=' + short + nl + 'next retained',
            'API_KEY="' + short + '"' + nl + 'next retained',
            'API_KEY=abc ' + short + nl + 'next retained',
        ])
    assert len(inputs) >= 12
    guarded = set()
    for text in inputs:
        old, new = old_order(text), redact(text)
        for piece in (first, rest, full, value):
            if piece in text and piece not in old:
                guarded.add(piece)
                assert piece not in new, (text, old, new)
    assert guarded == {first, rest, full, value}
