"""Strict verdict parse with recorded-reply fixtures and keeper compat."""
import sys
from pathlib import Path

import pytest

from plangraph.review import verdict as v

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import seat_verdict as sv  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "review"


def read(name):
    """Read one recorded reply fixture."""
    return (FIX / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name,want", [
    ("ok_pass.txt", "pass"),
    ("bold_verdict.txt", "pass"),
    ("no_without_zero_hits.txt", "pass"),
    ("length_partial.txt", None),
    ("fenced_verdict.txt", None),
    ("fix_with_findings.txt", "fix"),
    ("halt.txt", "halt"),
    ("echoed_template.txt", None),
    ("prose_verdict.txt", None),
])
def test_parse_verdict_fixtures(name, want):
    """Each recorded reply parses to its expected verdict."""
    assert v.parse_verdict(read(name)) is want


@pytest.mark.parametrize("name,want", [
    ("ok_pass.txt", "no"),
    ("bold_verdict.txt", "no"),
    ("no_without_zero_hits.txt", "missing"),
    ("fix_with_findings.txt", "no"),
    ("halt.txt", "no"),
    ("echoed_template.txt", "no"),
    ("prose_verdict.txt", "no"),
])
def test_parse_gaming_fixtures(name, want):
    """Gaming outcomes, including the malformed no without hit count."""
    outcome, quote = v.parse_test_gaming(read(name))
    assert outcome == want
    assert quote is None


def test_fixture_pass_allowed():
    """Only clean pass + stop + no may pass."""
    assert v.judge(read("ok_pass.txt"), "stop").pass_allowed is True
    assert v.judge(read("bold_verdict.txt"), "stop").pass_allowed is True
    for name in ("no_without_zero_hits.txt", "length_partial.txt",
                 "fenced_verdict.txt", "fix_with_findings.txt", "halt.txt",
                 "echoed_template.txt", "prose_verdict.txt"):
        assert v.judge(read(name), "stop").pass_allowed is False
    assert v.judge(read("ok_pass.txt"), "length").pass_allowed is False


def test_length_empty_inline():
    """Empty cut payload is empty, never passable."""
    payload = {"choices": [{"message": {"content": ""},
                             "finish_reason": "length"}]}
    assert v.reply_text(payload) == ""
    assert v.finish_reason(payload) == "length"
    got = v.judge(v.reply_text(payload), v.finish_reason(payload))
    assert got.empty is True
    assert got.verdict is None
    assert got.test_gaming == "missing"
    assert got.pass_allowed is False


def test_yes_needs_verbatim_quote():
    """A yes quote must occur verbatim in the material."""
    material = "first line\nneedle = 1\nlast line\n"
    yes = 'note\nTEST-GAMING: yes - "needle = 1"\nVERDICT: fix\n'
    assert v.parse_test_gaming(yes, material) == ("yes", "needle = 1")
    assert v.parse_test_gaming(yes, "other text\n") == (
        "yes_unverified_quote", "needle = 1")


def test_payload_shapes():
    """String and list-of-parts content both yield text."""
    assert v.reply_text({"choices": [{"message": {"content": "hi"},
                                      "finish_reason": "stop"}]}) == "hi"
    parts = {"choices": [{"message": {"content": [
        {"text": "a"}, {"nope": 1}, {"text": "b"}]},
        "finish_reason": "stop"}]}
    assert v.reply_text(parts) == "ab"
    assert v.reply_text({}) == ""
    assert v.finish_reason(parts) == "stop"
    assert v.finish_reason({}) is None


def test_strict_discipline():
    """Indent, hunks, and last-wins follow the strict rule."""
    assert v.parse_verdict("    VERDICT: pass\nVERDICT: fix\n") == "fix"
    assert v.parse_verdict("VERDICT: fix\n    VERDICT: pass\n") == "fix"
    hunk = "@@ -1,1 +1,1 @@\n+VERDICT: pass\nVERDICT: fix\n"
    assert v.parse_verdict(hunk) == "fix"
    assert v.parse_verdict("VERDICT: fix\nVERDICT: pass\n") == "pass"


def test_evidence_shape():
    """Evidence carries the three keys plus quote only for yes."""
    got = v.judge(read("ok_pass.txt"), "stop").to_evidence()
    assert set(got) == {"finish_reason", "verdict", "test_gaming"}
    material = "needle = 1\n"
    text = 'x\nTEST-GAMING: yes - "needle = 1"\nVERDICT: fix\n'
    quoted = v.judge(text, "stop", material).to_evidence()
    assert quoted["test_gaming_quote"] == "needle = 1"


def test_keeper_compat():
    """Pass here is pass there; clean fix/halt there is not pass."""
    for name in ("ok_pass.txt", "bold_verdict.txt",
                 "no_without_zero_hits.txt", "length_partial.txt",
                 "fenced_verdict.txt", "fix_with_findings.txt", "halt.txt",
                 "echoed_template.txt", "prose_verdict.txt"):
        text = read(name)
        got = v.judge(text, "stop")
        if got.pass_allowed:
            assert sv.parse_verdict(text)[0] is True
        if got.verdict in ("fix", "halt"):
            assert "```" not in text and "@@" not in text
            assert sv.parse_verdict(text)[0] is False
