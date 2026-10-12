"""D-887: ONE rule for chunks inside a path-exempt token (digit chunks).

A chunk holding any digit is secret-like unless it is a year ``(19|20)NN``, a
1-2 digit number (the MM/DD of a ``YYYY-MM-DD`` date, which splits into
separate chunks) or a version token ``v<N>``.  Letters-only chunks are clean,
apart from the pre-existing 12+ letter mixed-case CamelCase blob rule.

Synthetic fixtures only, built at runtime from fragments.  The leak oracle
asserts the refusal text EQUALS the template built from the hit span and the
masked text EQUALS ``text[:s] + "[redacted]" + text[e:]``, so no character of
a secret can survive in either output.
"""
import pytest

from plangraph.keeper.credential_guard import find_shapes, guard, mask
from plangraph.keeper.errors import Refused

_KW = "pass" + "word"
_LOGIN = "log" + "in"
_BLOB = "aBcDe" + "FgH"
_HEX32 = "ab12" * 8
_HEX31 = "ab12" * 7 + "ab1"
_B64_32 = "Zk9m" * 8
_B64_31 = "Zk9m" * 7 + "Zk9"
_QUOTED = ['password: "{v}"', "password = '{v}'", '{{"token": "{v}"}}',
           "secret: '{v}' (rotate)", 'api_key="{v}"']


def _keys():
    # the keyed-refusal file owns the pair / chunk / prose fixtures
    import test_keeper_credential_guard_keys as keys
    return keys


def _template(s, e, field="note", key=None):
    n = e - s
    who = f"item {key}: {field}" if key else field
    return (f"{who} looks like it contains a credential at chars {s}-{e} "
            f"({n} chars, masked {'*' * min(n, 8)}); remove or reword it")


def _refused(text, val=None, key=None):
    """Oracle: the message is only the template, the mask only [redacted]."""
    hits = find_shapes(text)
    assert hits, text
    s, e = hits[0].span()
    want = _template(s, e, "note", key)
    with pytest.raises(Refused) as exc:
        guard("note", text, key)
    assert exc.value.msg == want
    assert str(exc.value) == f"validation: {want}"
    masked, count = mask(text)
    assert count == len(hits)
    if len(hits) == 1:
        assert masked == text[:s] + "[redacted]" + text[e:]
    else:
        assert "[redacted]" in masked
    if val is not None:
        assert val not in text[:s] + text[e:]
        assert val not in masked
    return text


def _allowed(text):
    assert not find_shapes(text), text
    guard("note", text)
    assert mask(text) == (text, 0)


# -- the afbdd15 bypass and its mutation variants: every digit chunk counts --
_BYPASS = [
    _KW + " foo/bar/" + _BLOB + "-123.md",
    _KW + " foo/bar/" + _BLOB + "-1234.md",
    _KW + " foo/bar/" + _BLOB + "-1999x.md",
    _KW + " foo/bar/" + _BLOB + "_a1.md",
    _KW + " foo/bar/" + _BLOB + "-1aB1c.md",
    _KW + " foo/bar/" + _BLOB + "-123456.md",
    _KW + " foo/aBc-123/bar/readme.md",
]


@pytest.mark.parametrize("text", _BYPASS)
def test_digit_chunk_in_an_exempt_path_is_refused(text):
    _refused(text, val=_BLOB if _BLOB in text else None)


def test_digit_chunk_in_a_path_is_secret_like():
    # accepted false positive of the one rule: the "k3s" directory chunk
    # holds a digit, so the path is no longer exempt.
    _refused(_KW + " server/media/k3s-v1/readme.md", key="demo:X")


# -- year / 1-2 digit / v<N> chunks keep the path exempt --
@pytest.mark.parametrize("text", [
    _KW + " docs/incidents/2026-08-30-notes.md",
    _KW + " docs/incidents/1999-notes.md",
    _KW + " docs/incidents/file_2026-08-30.md",
    _KW + " " + _keys()._op1(),
    _KW + " " + _keys()._docker(),
    _LOGIN + " docs/runbook/docker-compose-v2.yml",
    _LOGIN + " docs/runbook/docker-compose_v10.yml",
    _LOGIN + " docs/incidents/report_v2.md",
    _LOGIN + " docs/incidents/chapter-part-7.md",
    _LOGIN + " docs/2026-08-30-ups-notes/readme.md",
])
def test_year_small_number_and_version_chunks_stay_paths(text):
    _allowed(text)


@pytest.mark.parametrize("text", [
    _KW + " docs/incidents/1234-notes.md",
    _KW + " docs/incidents/1999x-notes.md",
    _KW + " docs/incidents/v1234-notes.md",
    _KW + " docs/incidents/20268-notes.md",
])
def test_four_digits_are_a_year_only_in_the_19xx_20xx_range(text):
    _refused(text)


# -- documented accepted limit: letters-only path chunks are not secrets --
@pytest.mark.parametrize("text", [
    _LOGIN + " a/b/correct-horse-battery-staple.md",
    _LOGIN + " a/b/correcthorsebattery.md",
])
def test_letters_only_passphrase_path_is_an_accepted_limit(text):
    # a change here must come with a design decision, not a heuristic
    _allowed(text)


def test_long_mixed_case_letters_only_blob_stays_secret():
    _refused(_LOGIN + " a/b/" + "Correct" + "HorseBattery.md", val="HorseBattery")


# -- runs: keyword-gated, length- and entropy-gated --
def _padded(start, run):
    # "token" sits at char 0; *run* starts exactly at char *start*
    return "token" + "." * (start - 6) + " " + run


def test_hex_run_needs_32_chars_and_a_keyword():
    _refused(_LOGIN + " " + _HEX32, val=_HEX32)
    _allowed(_LOGIN + " " + _HEX31)
    _allowed("readme " + _HEX32)          # keyword-gated by spec
    _refused(_padded(60, _HEX32), val=_HEX32)
    _allowed(_padded(61, _HEX32))


def test_base64_run_needs_32_chars_and_entropy():
    _refused(_LOGIN + " " + _B64_32, val=_B64_32)
    _allowed(_LOGIN + " " + _B64_31)
    _allowed(_LOGIN + " " + "k" * 32)
    _allowed(_LOGIN + " " + "kmnp" * 8)


def test_hex_run_in_a_path_token_is_refused_by_the_digit_rule():
    _refused(_LOGIN + " docs/" + _HEX32 + ".md", val=_HEX32)


# -- the oracle over the fixtures the keyed file already owns --
def test_oracle_on_the_existing_refusal_fixtures():
    keys = _keys()
    for text, val in zip(keys._bad_texts(), keys._bad_vals()):
        _refused(text, val=val)
    for text, val in zip(keys._pair_secret_texts(), keys._pair_secret_vals()):
        _refused(text, val=val)
    for text in keys._chunk_bad_texts():
        _refused(text)
    for template in _QUOTED:
        _refused(template.format(v=keys._quoted_value()),
                 val=keys._quoted_value())


def test_oracle_on_the_existing_pass_fixtures():
    keys = _keys()
    for text in keys._prose_texts():
        _allowed(text)
    _allowed(keys._chunk_pass_text())
    _allowed(_KW + " " + keys._docker())
    _allowed("see " + keys._op1() + " for the " + _KW)
