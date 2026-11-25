"""Verdict parse in parity with the keeper tool, plus fixtures."""
import json
import inspect
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


def test_golden_verdict_words():
    cases = json.loads((FIX.parent / "verdict_words.json").read_text())
    outcomes = {"pass": True, "block": False, "none": None}
    for case in cases:
        assert case["expect"] == case["new_door"]
        assert sv.parse_verdict(case["text"])[0] is outcomes[case["new_door"]], case
        assert to_bool(v.parse_verdict(case["text"])) is outcomes[case["new_twin"]], case


def test_golden_old_outcomes_and_changed_sets():
    # Copied from origin/main tools/seat_verdict.py:54-55 and
    # src/plangraph/review/verdict.py:27-28, not the design vocabulary.
    old_sets = {
        "door": ({"pass", "approve", "accept"},
                 {"fix", "halt", "change", "changes", "reject"}),
        "twin": ({"pass", "approve", "accept"},
                 {"fix", "halt", "change", "changes", "reject"}),
    }
    cases = json.loads((FIX.parent / "verdict_words.json").read_text())
    for parser, (passes, blocks) in old_sets.items():
        changed = set()
        for case in cases:
            assert set(case) == {"text", "word", "expect", "old_door",
                                 "new_door", "old_twin", "new_twin"}
            for field in ("old_door", "new_door", "old_twin", "new_twin"):
                assert case[field] in {"pass", "block", "none"}
            word = case["word"]
            if word is None:
                continue
            # Old first-word matcher reads both NO GO and NO-GO as 'no'.
            first = "no" if word == "no-go" else word
            old = ("block" if word == "request_changes" or first in blocks
                   else "pass" if first in passes else "none")
            assert case["old_" + parser] == old, case
            if old != case["new_" + parser]:
                changed.add(word)
        assert changed == {"fail", "no", "no-go", "blocked", "go"}


def test_no_go_without_whitespace_substitution():
    for module in (sv, v):
        assert 're.sub(r"\\s+", "-", word)' not in inspect.getsource(module)
    cases = json.loads((FIX.parent / "verdict_words.json").read_text())
    case = next(case for case in cases if case["text"] == "VERDICT: NO GO")
    assert case["new_door"] == case["new_twin"] == "block"
    assert sv.parse_verdict(case["text"])[0] is False
    assert v.parse_verdict(case["text"]) == "fix"


@pytest.mark.parametrize("finish,gaming,want", [
    ("stop", "no", 0), ("end_turn", "no", 0),
    ("length", "no", 4), ("stop", "yes", 4),
    ("stop", "unsure", 4), ("stop", None, 4),
])
def test_go_keeps_pass_safety_gates(tmp_path, finish, gaming, want):
    from test_seat_verdict import run

    assert run(tmp_path, "go", "VERDICT: go", finish_reason=finish,
               test_gaming=gaming, extra=("--dry-run",)) == want


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
    "VERDICT: GO",
    "VERDICT: FAIL - 2 findings",
    "VERDICT: NO GO",
    "VERDICT: approve",
    "VERDICT: reject.",
    "VERDICT: Changes requested",
    "@@ -1,1 +1,1 @@\n VERDICT: pass",
    "\tVERDICT: pass",
    "VERDICT: PASS",
    "notes here\nverdict: fix - bad flush\n",
    "VERDICT: change the code",
    "VERDICT:",
    "VERDICT: fix\nVERDICT: <pass|fix>",
    "VERDICT: <pass|fix>",
    "```\nVERDICT: pass\n```",
    "VERDICT: fix\n> VERDICT: pass",
]

_CORPUS = list(_INLINE_CORPUS) + [
    (FIX / name).read_text(encoding="utf-8") for name in _FIXTURE_NAMES
]


@pytest.mark.parametrize("name,want", [
    ("ok_pass.txt", "pass"),
    ("bold_verdict.txt", "pass"),
    ("no_without_zero_hits.txt", "pass"),
    ("length_partial.txt", None),
    ("fenced_verdict.txt", None),
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
    for name in ("ok_pass.txt", "bold_verdict.txt",
                 "prose_verdict.txt"):
        assert v.judge(read(name), "stop").pass_allowed is True
    for name in ("no_without_zero_hits.txt", "fix_with_findings.txt",
                 "halt.txt", "echoed_template.txt", "fenced_verdict.txt",
                 "length_partial.txt"):
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
    """Last VERDICT line wins; indent/hunk lines count, fenced/quoted never decide."""
    assert v.parse_verdict("VERDICT: fix\nVERDICT: pass\n") == "pass"
    assert v.parse_verdict("VERDICT: pass\nVERDICT: fix\n") == "fix"
    assert v.parse_verdict("    VERDICT: pass\nVERDICT: fix\n") == "fix"
    assert v.parse_verdict("VERDICT: fix\n    VERDICT: pass\n") == "pass"
    hunk = "@@ -1,1 +1,1 @@\n+VERDICT: pass\nVERDICT: fix\n"
    assert v.parse_verdict(hunk) == "fix"
    assert v.parse_verdict("VERDICT: fix\n```\nVERDICT: pass\n```\n") == "fix"


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


BRIEF_FINDING = ("PROCESS src/plangraph/review/review_brief.md:26 field=public_symbols - the supplied brief "
                 "still omits the required per-item public symbols")
EMPTY_PROMPT = "=== ITEM CONTEXT\nItem key: x\nChanged public symbols: \n"
LANE_DIFF = "diff --git a/src/a.py b/src/a.py\n+x\n"


def test_finding_about_the_review_brief_is_process_not_med():
    review = f"FILES READ: src/a.py\n{BRIEF_FINDING}\nVERDICT: REQUEST_CHANGES\n"
    found = v.classify_findings(review, prompt=EMPTY_PROMPT + LANE_DIFF,
                                changed_paths=v.diff_paths(LANE_DIFF))
    assert (found.high, found.med, found.blocking) == (0, 0, 0)
    assert found.process == (BRIEF_FINDING,)


def test_process_class_needs_the_defect_in_the_prompt_and_an_untouched_file():
    review = f"{BRIEF_FINDING}\nHIGH src/a.py:3 - real\nVERDICT: REQUEST_CHANGES\n"
    ok_prompt = EMPTY_PROMPT.replace("symbols: \n", "symbols: run\n")
    ok_prompt += "Callers of changed public symbols: a\nAffected test output (paths): l\n"
    # the prompt carries the block: the finding is an ordinary MED
    assert v.classify_findings(review, prompt=ok_prompt + LANE_DIFF,
                               changed_paths={"src/a.py"}).med == 1
    # unknown diff scope fails closed
    assert v.classify_findings(review, prompt=EMPTY_PROMPT, changed_paths=None).med == 1
    # the head changes the brief: a finding about it is a real finding
    touched = {"src/plangraph/review/review_brief.md"}
    assert v.classify_findings(review, prompt=EMPTY_PROMPT, changed_paths=touched).blocking == 2
    # mixed: the real HIGH stays, the brief finding goes to PROCESS
    mixed = v.classify_findings(review, prompt=EMPTY_PROMPT, changed_paths={"src/a.py"})
    assert (mixed.high, mixed.med, len(mixed.process)) == (1, 0, 1)


def test_typed_process_line_that_fails_verification_counts_as_med():
    review = "PROCESS skills/review/SKILL.md:1 field=callers - the supplied brief omits the callers\n> MED quoted a.py:1 - x\n"
    assert v.classify_findings(review).med == 1
    found = v.classify_findings(review, prompt=EMPTY_PROMPT, changed_paths={"a.py"})
    assert (found.med, len(found.process)) == (0, 1)
    vague = "PROCESS skills/review/SKILL.md:1 - rubric missing\n"
    assert v.classify_findings(vague, prompt=EMPTY_PROMPT, changed_paths={"a.py"}).med == 1


def test_diff_paths_reads_the_dispatched_git_diff_in_the_seat_checkout(tmp_path):
    import subprocess

    def git(*args):
        return subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t",
                               *args], check=True, capture_output=True, text=True).stdout.strip()

    git("init", "-q")
    (tmp_path / "a.txt").write_text("1")
    git("add", "."); git("commit", "-qm", "one")
    base = git("rev-parse", "HEAD")
    (tmp_path / "b.txt").write_text("2")
    git("add", "."); git("commit", "-qm", "two")
    head = git("rev-parse", "HEAD")
    prompt = f"read the review diff, run:\ngit diff {base} {head}\n"
    assert v.diff_paths(prompt, str(tmp_path)) == {"b.txt"}
    assert v.diff_paths(prompt) is None  # no checkout: scope unknown, fail closed
    assert v.diff_paths("no diff here") is None


def test_process_finding_must_name_an_empty_field_and_have_the_brief_as_its_file():
    prompt = EMPTY_PROMPT + LANE_DIFF
    scope = {"src/a.py"}
    advice = "MED src/a.py:3 - symbols callers wrong; see src/plangraph/review/review_brief.md:26"
    assert v.classify_findings(advice, prompt=prompt, changed_paths=scope).med == 1
    other = "MED src/plangraph/review/review_brief.md:12 - the rubric wording is vague"
    assert v.classify_findings(other, prompt=prompt, changed_paths=scope).med == 1


def test_item_context_check_stops_at_the_end_of_the_rendered_block():
    from plangraph.review.prompt import item_context_defects, render_brief, verified_empty

    context = {"item_key": "k", "public_symbols": "run", "callers": verified_empty("x"),
               "test_output_paths": "l.log"}
    brief = render_brief("review", context, require_item_context=True)
    prose = "\n=== NOTES\nthe heading === ITEM CONTEXT is described here\nChanged public symbols: \n"
    assert item_context_defects(brief + prose) == []
    assert item_context_defects(brief + "\nsee === ITEM CONTEXT above") == []


def test_untyped_findings_are_never_process_whatever_they_say():
    prompt, scope = EMPTY_PROMPT + LANE_DIFF, {"src/a.py"}
    for line in ("HIGH src/plangraph/review/review_brief.md:12 - rubric wording is vague; fix by adding callers",
                 "HIGH src/plangraph/review/review_brief.md:12 - the rubric uses empty examples of symbols",
                 "MED src/plangraph/review/review_brief.md:26 - the brief omits the callers of changed symbols"):
        found = v.classify_findings(line, prompt=prompt, changed_paths=scope)
        assert (found.blocking, found.process) == (1, ()), line


def test_typed_process_line_names_one_context_field_the_prompt_lacks():
    prompt, scope = EMPTY_PROMPT + LANE_DIFF, {"src/a.py"}
    ok = "PROCESS review_brief.md field=callers - no callers rendered"
    assert v.classify_findings(ok, prompt=prompt, changed_paths=scope).process == (ok,)
    full = EMPTY_PROMPT.replace("symbols: \n", "symbols: run\n") + "Callers of changed public symbols: a\n" \
        "Affected test output (paths): l\n"
    assert v.classify_findings(ok, prompt=full, changed_paths=scope).med == 1  # field is rendered
    assert v.classify_findings("PROCESS review_brief.md - no field", prompt=prompt, changed_paths=scope).med == 1
    assert v.classify_findings("PROCESS src/a.py field=callers - wrong file", prompt=prompt, changed_paths=scope).med == 1


@pytest.mark.parametrize("line", ["HIGH ```", "MED `", "- MED ``` ", "HIGH ", "P2 \"\""])
def test_a_finding_with_an_empty_citation_keeps_its_severity(line):
    found = v.classify_findings(line + "\n", prompt=EMPTY_PROMPT, changed_paths={"a.py"})
    assert (found.blocking, found.process) == (1, ())


def test_bound_diff_paths_needs_the_recorded_digest(tmp_path):
    import subprocess

    def git(*args):
        return subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t",
                               *args], check=True, capture_output=True, text=True).stdout.strip()

    git("init", "-q")
    (tmp_path / "a.txt").write_text("1")
    git("add", "."); git("commit", "-qm", "one")
    base = git("rev-parse", "HEAD")
    (tmp_path / "b.txt").write_text("2")
    (tmp_path / "a.txt").write_text("3")
    git("add", "."); git("commit", "-qm", "two")
    head = git("rev-parse", "HEAD")
    digest, paths = v.diff_binding(base, head, str(tmp_path))
    assert paths == {"a.txt", "b.txt"}
    binding = {"base_sha": base, "head_sha": head, "diff_sha256": digest}
    assert v.bound_diff_paths(binding, str(tmp_path)) == paths
    assert v.bound_diff_paths(dict(binding, diff_sha256="0" * 64), str(tmp_path)) is None
    assert v.bound_diff_paths(binding, None) is None and v.bound_diff_paths(None, str(tmp_path)) is None
