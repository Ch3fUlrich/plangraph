"""Verdict parse in parity with the keeper tool, plus fixtures."""
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


def to_bool(word):
    """Map a verdict word to the keeper boolean (None stays None)."""
    if word is None:
        return None
    return word == "pass"


_FIXTURE_NAMES = [
    "ok_pass.txt",
    "bold_verdict.txt",
    "no_without_zero_hits.txt",
    "length_partial.txt",
    "fenced_verdict.txt",
    "fix_with_findings.txt",
    "halt.txt",
    "echoed_template.txt",
    "prose_verdict.txt",
]

_INLINE_CORPUS = [
    "notes\nVERDICT: fix\nmore\n```\nVERDICT: pass\n```\n",
    "VERDICT: fix\n```\nVERDICT: pass\n```",
    "`VERDICT: pass`",
    "**VERDICT: pass**",
    "**Verdict:** approve",
    "VERDICT: pass | fix | halt",
    "VERDICT: pass/fix",
    "> VERDICT: pass",
    "## Verdict: FIX",
    "VERDICT: pass, but the docs need a follow-up",
    "    VERDICT: pass",
    "VERDICT: maybe",
    "",
    "looks good, no decision here",
    "VERDICT: fix\nVERDICT: pass",
    "VERDICT: pass\nVERDICT: fix",
    "VERDICT: halt",
    "VERDICT: approve",
    "VERDICT: reject.",
    "VERDICT: Changes requested",
    "@@ -1,1 +1,1 @@\n VERDICT: pass",
    "\tVERDICT: pass",
    "VERDICT: PASS",
    "notes here\nverdict: fix - bad flush\n",
    "VERDICT: change the code",
    "VERDICT:",
]

_CORPUS = list(_INLINE_CORPUS) + [
    (FIX / name).read_text(encoding="utf-8") for name in _FIXTURE_NAMES
]


@pytest.mark.parametrize("name,want", [
    ("ok_pass.txt", "pass"),
    ("bold_verdict.txt", "pass"),
    ("no_without_zero_hits.txt", "pass"),
    ("length_partial.txt", "pass"),
    ("fenced_verdict.txt", "pass"),
    ("fix_with_findings.txt", "fix"),
    ("halt.txt", "halt"),
    ("echoed_template.txt", None),
    ("prose_verdict.txt", "pass"),
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
    """Pass needs verdict pass + stop + gaming no."""
    for name in ("ok_pass.txt", "bold_verdict.txt", "length_partial.txt",
                 "fenced_verdict.txt", "prose_verdict.txt"):
        assert v.judge(read(name), "stop").pass_allowed is True
    for name in ("no_without_zero_hits.txt", "fix_with_findings.txt",
                 "halt.txt", "echoed_template.txt"):
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


def test_last_wins_and_all_lines_count():
    """Last VERDICT line wins; fence/indent/hunk lines all count."""
    assert v.parse_verdict("VERDICT: fix\nVERDICT: pass\n") == "pass"
    assert v.parse_verdict("VERDICT: pass\nVERDICT: fix\n") == "fix"
    assert v.parse_verdict("    VERDICT: pass\nVERDICT: fix\n") == "fix"
    assert v.parse_verdict("VERDICT: fix\n    VERDICT: pass\n") == "pass"
    hunk = "@@ -1,1 +1,1 @@\n+VERDICT: pass\nVERDICT: fix\n"
    assert v.parse_verdict(hunk) == "fix"
    assert v.parse_verdict("VERDICT: fix\n```\nVERDICT: pass\n```\n") == "pass"


def test_evidence_shape():
    """Evidence carries the three keys plus quote only for yes."""
    got = v.judge(read("ok_pass.txt"), "stop").to_evidence()
    assert set(got) == {"finish_reason", "verdict", "test_gaming"}
    material = "needle = 1\n"
    text = 'x\nTEST-GAMING: yes - "needle = 1"\nVERDICT: fix\n'
    quoted = v.judge(text, "stop", material).to_evidence()
    assert quoted["test_gaming_quote"] == "needle = 1"


def test_keeper_compat():
    """Judged verdicts agree with the keeper tool parse."""
    for name in _FIXTURE_NAMES:
        text = read(name)
        got = v.judge(text, "stop")
        assert to_bool(got.verdict) is sv.parse_verdict(text)[0]
        if got.pass_allowed:
            assert sv.parse_verdict(text)[0] is True
        if got.verdict in ("fix", "halt"):
            assert sv.parse_verdict(text)[0] is False


@pytest.mark.parametrize("text", _CORPUS)
def test_verdict_parity_with_seat_verdict(text):
    """Every reply decides exactly like the keeper tool."""
    assert to_bool(v.parse_verdict(text)) is sv.parse_verdict(text)[0]


def test_halt_and_fix_map_to_false():
    """Both halt and fix are keeper False, pass is True."""
    assert to_bool(v.parse_verdict("VERDICT: halt")) is False
    assert to_bool(v.parse_verdict("VERDICT: fix")) is False
    assert to_bool(v.parse_verdict("VERDICT: pass")) is True
    assert sv.parse_verdict("VERDICT: halt")[0] is False
    assert sv.parse_verdict("VERDICT: fix")[0] is False


LOW_SEVERITY_TABLE = [
    ("VERDICT: fix - LOW: stale test name", "pass", True),
    ("VERDICT: fix - LOW: x; LOW: y", "pass", True),
    ("VERDICT: fix - Low: a; nit: b", "pass", True),
    ("VERDICT: fix - MED: a. LOW: b", "fix", False),
    ("VERDICT: fix - High: a; Medium: b; Low: c", "fix", False),
    ("VERDICT: fix - LOW: no test for the MED path", "pass", True),
    ("VERDICT: fix", "fix", False),
    ("VERDICT: fix - add a test", "fix", False),
    ("VERDICT: halt - LOW: x", "halt", False),
    ("VERDICT: reject - LOW: x", "fix", False),
    ("VERDICT: pass", "pass", False),
    ("VERDICT: fix - LOW: x\nVERDICT: fix - MED: y", "fix", False),
    ("VERDICT: pass | fix - LOW", None, False),
    # markdown-bold/emphasised/code severity labels (F1)
    ("VERDICT: fix - LOW: a; **MED**: b", "fix", False),
    ("VERDICT: fix - **LOW**: a", "pass", True),
    ("VERDICT: fix - `HIGH`: a; LOW: b", "fix", False),
    ("VERDICT: fix - _Medium_ (b); low: a", "fix", False),
    ("VERDICT: fix - **low** (a)", "pass", True),
    # dash-separated blocking labels fail closed (L1-1)
    ("VERDICT: fix - LOW: x; Medium - y", "fix", False),
    ("VERDICT: fix - LOW: x; HIGH - y", "fix", False),
    ("VERDICT: fix - LOW: x; **MED** — y", "fix", False),
    ("VERDICT: fix - LOW: medication typo", "pass", True),
    (
        "VERDICT: fix - LOW: CHANGELOG.md D-2773 entry still says \"admitted in batch by design\", "
        "contradicting the updated docs/REVIEW-METHOD.md (\"admitted in any mode\"). "
        "Non-blocking for the fix itself. Test run output was not provided, so the pass claim rests on reading the code.",
        "pass",
        True,
    ),
    (
        "VERDICT: fix - low: stale test name `test_s1_lost_is_sticky_until_boundary` contradicts "
        "the new \"permanent\" semantics (V3); low: a partial `send_response` failure can leave "
        "a status line in `_headers_buffer`, so the guard could send a second one, and the test stub "
        "does not exercise this (V2); not verified: the escape-guard body, `keeper_util.seed`, "
        "and the 503 branch for lost-before-COMMIT are outside the material.",
        "pass",
        True,
    ),
    ("VERDICT: fix - LOW only: substring name match at tools/affected_tests.py:149 can suppress the full-suite fallback; missing tests for browser-only empty selection", "pass", True),
    ("VERDICT: fix - **LOW only**: a", "pass", True),
    ("VERDICT: fix - LOW only: a; MED: b", "fix", False),
]


@pytest.mark.parametrize("text,want_verdict,is_low_only", LOW_SEVERITY_TABLE)
def test_low_severity_verdict_table(text, want_verdict, is_low_only):
    assert v.parse_verdict(text) == want_verdict
    assert sv.parse_verdict(text)[0] is to_bool(want_verdict)
    ev = v.judge(text, "stop").to_evidence()
    if is_low_only:
        assert ev.get("low_only") is True
    else:
        assert "low_only" not in ev


def test_standing_answer_no_tolerates_scope_quotes_and_still_needs_zero_hits():
    assert v.parse_test_gaming(
        'TEST-GAMING: no - searched the "batch in" stamp: 0 hits\nVERDICT: pass'
    ) == ("no", None)
    assert v.parse_test_gaming(
        'TEST-GAMING: no - searched the "batch in" stamp: 3 hits'
    )[0] != "no"

