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
    # Keep the regression's stable ID; MY_SECRET's scope now agrees with Rust.
    value = 'hunter' + '2value'
    for sep in (': ', '='):
        standalone = 'MY_' + 'SECRET' + sep + value
        assert redact(standalone) == 'MY_SECRET' + sep + '[REDACTED]'
        for nl in ('\n', '\r\n'):
            text = 'x ' + 'gh' + 'p_' + 'A9' * 7 + nl + standalone
            assert redact(text) == 'x [REDACTED]' + sep + '[REDACTED]'


@pytest.mark.parametrize('name', ['MY_SECRET', 'DB_PASSWORD', 'AUTH_TOKEN', 'DB_PASSWD',
                                  'APIKEY', 'ACCESSKEY', 'PRIVATEKEY', 'CREDENTIALS',
                                  'max_tokens', 'token_count'])
@pytest.mark.parametrize('nl', ['\n', '\r\n'])
def test_widened_names_are_shielded_before_wrapped_join(name, nl):
    short = 'gh' + 'p_' + 'A9' * 7
    text = 'x ' + short + nl + '"' + name + '" : v'
    assert env_redact.join_shielded_assignments(text, env_redact._ASSIGNMENT) == text
    assert redact(text) == 'x ' + short + nl + '"' + name + '" : [REDACTED]'
    text = name + '=' + short + nl + 'B8' * 8 + nl + 'next retained'
    expected = name + '=[REDACTED]' + nl + 'next retained'
    assert env_redact.join_shielded_assignments(text, env_redact._ASSIGNMENT) == expected
    assert redact(text) == expected


def main_order(text):
    """Main's primitives/order, without P; inputs need no PEM/Bearer rules."""
    text = redact_wrapped(env_redact._redact_assignments(text))
    text = re.sub(r'(\b[A-Za-z][A-Za-z0-9+.-]*://[^\s/@:\x22\x27]+:)[^\s\x22\x27]*(@)',
                  r'\1[REDACTED]\2', text)
    return JWT.sub('[REDACTED]', TOKENS.sub('[REDACTED]', text))


def test_plain_order_never_reveals_pieces_masked_by_old_order():
    first, rest, value = 'A9' * 7, 'B8' * 8, 'hunter' + '2value'
    tiny_body, extra = 'D6' * 4 + 'D', 'C7' * 3
    short = 'gh' + 'p_' + first
    full, tiny = short + extra, 'gh' + 'p_' + tiny_body
    pieces = (first, first + extra, extra, rest, tiny_body, value)
    inputs, defects = [], []
    for nl in ('\n', '\r\n'):
        for line1 in (short, full, 'API_KEY=' + short, 'API_KEY="' + short, 'ordinary'):
            for line2 in (rest, 'PASSWORD=' + tiny, 'PASSWORD="' + tiny + '"',
                          'PASSWORD=' + value, 'next line of prose'):
                for line3 in ('', nl + 'next retained', nl + rest):
                    inputs.append(line1 + nl + line2 + line3)
        defects.extend(['API_KEY=' + short + nl + rest + nl + 'next',
                        'API_KEY="' + short + nl + rest + '"' + nl + 'next retained'])
    assert len(inputs) >= 40
    guarded = set()
    for text in inputs + defects:
        old, new = main_order(text), redact(text)
        for piece in pieces:
            if piece in text and piece not in old:
                guarded.add(piece)
                assert piece not in new, (text, old, new)
        if text in defects:
            assert rest not in new, (text, new)
    assert guarded == set(pieces)


@pytest.mark.parametrize('marker', ['\ue000', '\ue001', '\ue0000\ue001'])
@pytest.mark.parametrize('nl', ['\n', '\r\n'])
def test_shield_markers_skip_prepass_and_equal_main(marker, nl):
    first, rest = 'A9' * 7, 'B8' * 8
    text = marker + ' API_KEY=' + 'gh' + 'p_' + first + nl + rest + nl + 'next'
    assert env_redact.join_shielded_assignments(text, env_redact._ASSIGNMENT) == text
    assert redact(text) == main_order(text)


def test_shielded_join_preserves_all_names_separators_and_values():
    # Distinct indices (including two digits), Unicode and quoted names restore exactly.
    text = '\n'.join('"API_KEY" : value' + str(i) for i in range(12))
    assert env_redact.join_shielded_assignments(text, env_redact._ASSIGNMENT) == text
    for nl in ('\n', '\r\n'):
        text = 'API_KEY=' + 'gh' + 'p_' + 'A9' * 7 + nl + 'B8' * 8 + nl + 'next retained'
        assert env_redact.join_shielded_assignments(text, env_redact._ASSIGNMENT) == (
            'API_KEY=[REDACTED]' + nl + 'next retained')


@pytest.mark.parametrize('nl', ['\n', '\r\n'])
@pytest.mark.parametrize('quote', ['', '"', "'"])
def test_earlier_short_token_cannot_expose_below_floor_assignment(nl, quote):
    first, tiny_body = 'A9' * 7, 'D6' * 4 + 'D'
    short, tiny = 'gh' + 'p_' + first, 'gh' + 'p_' + tiny_body
    for lead, name, ending in [('x ', 'API_KEY', ''), ('x ', 'API_KEY', nl + 'end'),
                               ('', 'PASSWORD', '')]:
        text = lead + short + nl + name + '=' + quote + tiny + quote + ending
        expected = lead + '[REDACTED]=' + quote + '[REDACTED]' + quote + ending
        assert redact(text) == main_order(text) == expected
        assert tiny_body not in env_redact.redact_output(text)
