"""planner.readiness: the code-only readiness gate (hermetic, tmp_path only)."""
import os
import stat

import pytest

from plangraph.planner import readiness as rd
from plangraph.planner.readiness import (
    USABLE_FRACTION, WINDOW_TOKENS, Finding, Verdict, check_card, check_cards,
    default_dry_run, paths_overlap, write_sets_disjoint)


def ok_run(cmd, root):
    return True, "ok"


def bad_run(cmd, root):
    return False, "nope-reason"


def good_card(**over):
    card = {"outcome": "do it", "read_set": [], "write_set": ["src/a.py"], "exports": [],
            "forbid_paths": [], "smoke_checks": [], "check_cmd": "true",
            "heldout_check": None, "window_class": "128k", "risk_tags": [],
            "estimate_tokens": 1000}
    card.update(over)
    return card


def gate(tmp_path, card, **kw):
    kw.setdefault("dry_run", ok_run)
    return check_card(card, repo_root=tmp_path, **kw)


# ------------------------------------------------------------------ constants

def test_constants():
    assert WINDOW_TOKENS == {"128k": 131072, "256k": 262144, "1m": 1048576}
    assert USABLE_FRACTION == 0.55
    assert set(rd.CODES) >= {"no_check", "check_not_runnable", "read_missing",
                             "write_overlap", "estimate_missing", "estimate_too_big",
                             "leg_request_too_big", "no_risk_tags", "no_window_class",
                             "bad_path"}


# ---------------------------------------------------------------- paths_overlap

OVERLAP = [
    ("src/a.py", "src/a.py", True),
    ("src", "src/a.py", True),
    ("src/*.py", "src/a.py", True),
    ("src/*.py", "tests/a.py", False),
    ("src/pkg/**", "src/pkg/x/y.py", True),
    ("src/a/**", "src/b/**", False),
    ("**/x.py", "src/x.py", True),
    ("docs/", "src", False),
    ("./src/a.py", "src/a.py", True),
    ("src/", "src/a.py", True),
    ("src/a", "src/ab/c.py", False),
    ("src/pkg/**", "src/pkg", True),
    ("src/**/x.py", "src/x.py", True),
    ("src/a/*.py", "src/a/**", True),
    ("src/a/*.py", "tests/**", False),
]


@pytest.mark.parametrize("a,b,want", OVERLAP)
def test_paths_overlap_table(a, b, want):
    assert paths_overlap(a, b) is want
    assert paths_overlap(b, a) is want


def test_write_sets_disjoint_reports_pairs():
    others = {"k2": ["tests/**", "src/a.py"], "k3": ["docs"]}
    assert write_sets_disjoint(["src/a.py", "docs/x.md"], others) == [
        ("src/a.py", "k2", "src/a.py"), ("docs/x.md", "k3", "docs")]
    assert write_sets_disjoint(["src/b.py"], others) == []
    assert write_sets_disjoint([], others) == []


# -------------------------------------------------------------- default_dry_run

def test_dry_run_script_file_and_unknown_binary(tmp_path):
    (tmp_path / "scripts").mkdir()
    script = tmp_path / "scripts" / "check.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    assert default_dry_run("scripts/check.sh --fast", tmp_path) == (True, "ok")
    assert default_dry_run("scripts/missing.sh", tmp_path) == (
        False, "executable not found: scripts/missing.sh")
    assert default_dry_run("no-such-binary-xyz arg", tmp_path) == (
        False, "executable not found: no-such-binary-xyz")


def test_dry_run_does_not_execute(tmp_path):
    (tmp_path / "run.sh").write_text("touch ran.flag\n")
    assert default_dry_run("./run.sh", tmp_path)[0] is True
    assert not (tmp_path / "ran.flag").exists()


def test_dry_run_pytest_paths(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("")
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "pytest").write_text("")
    assert default_dry_run("python3 -m pytest -q tests/test_a.py", tmp_path) == (True, "ok")
    assert default_dry_run("python3 -m pytest -q tests/test_a.py::test_x -k foo",
                           tmp_path) == (True, "ok")
    assert default_dry_run("python3 -m pytest tests/test_b.py", tmp_path) == (
        False, "missing test path: tests/test_b.py")
    assert default_dry_run("python3 -m pytest -q tests/test_b.py::test_x", tmp_path) == (
        False, "missing test path: tests/test_b.py")
    assert default_dry_run("bin/pytest -q tests/nodir/", tmp_path) == (
        False, "missing test path: tests/nodir/")
    assert default_dry_run("FOO=1 BAR=2 python3 -m pytest -q", tmp_path) == (True, "ok")


def test_dry_run_segments_and_parse_errors(tmp_path):
    assert default_dry_run("true && no-such-binary-xyz", tmp_path) == (
        False, "executable not found: no-such-binary-xyz")
    assert default_dry_run("true; true | true", tmp_path) == (True, "ok")
    assert default_dry_run("echo $(no-such-binary-xyz)", tmp_path)[0] is False
    # only the first five segments are inspected
    assert default_dry_run("true;true;true;true;true;no-such-binary-xyz", tmp_path) == (
        True, "ok")
    ok, reason = default_dry_run("echo 'unterminated", tmp_path)
    assert ok is False and reason
    assert default_dry_run("   ", tmp_path)[0] is False


# ------------------------------------------------------------------ check_card

def test_good_card_is_ready(tmp_path):
    v = gate(tmp_path, good_card(), key="k1")
    assert v == Verdict("k1", ())
    assert v.ready and v.codes() == [] and v.summary() == "k1: ready"


def test_no_check(tmp_path):
    for cmd in (None, "", "   "):
        assert gate(tmp_path, good_card(check_cmd=cmd)).codes() == ["no_check"]
    card = good_card()
    del card["check_cmd"]
    assert gate(tmp_path, card).codes() == ["no_check"]


def test_check_not_runnable_carries_reason(tmp_path):
    v = gate(tmp_path, good_card(), dry_run=bad_run)
    assert v.codes() == ["check_not_runnable"]
    assert "nope-reason" in v.findings[0].message


def test_default_dry_run_used_when_not_injected(tmp_path):
    v = check_card(good_card(check_cmd="no-such-binary-xyz"), repo_root=tmp_path)
    assert v.codes() == ["check_not_runnable"]
    assert "executable not found" in v.findings[0].message


def test_read_missing_plain_and_glob(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "d.md").write_text("")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("")
    ok = gate(tmp_path, good_card(read_set=["docs", "src/a.py", "src/*.py", "docs/**/*.md"]))
    assert ok.ready
    v = gate(tmp_path, good_card(read_set=["gone.txt", "docs", "nope/*.py", "src/b.py"]))
    assert v.codes() == ["read_missing"]
    assert [f.message for f in v.findings] == ["gone.txt", "nope/*.py", "src/b.py"]


def test_bad_path_in_all_three_lists(tmp_path):
    card = good_card(write_set=["/etc/x"], read_set=["../up"],
                     forbid_paths=["a/../b"])
    v = gate(tmp_path, card)
    assert v.codes() == ["bad_path"]
    assert len(v.findings) == 3
    assert {f.message.split(":")[0] for f in v.findings} == {
        "write_set", "read_set", "forbid_paths"}
    # a bad read entry is not also reported missing; dotted names are fine
    assert gate(tmp_path, good_card(write_set=["src/a..b.py"])).ready


def test_write_overlap_with_running(tmp_path):
    running = {"other": ["src/a.py", "docs"], "far": ["tests/**"]}
    v = gate(tmp_path, good_card(), key="me", running_write_sets=running)
    assert v.codes() == ["write_overlap"]
    assert v.findings[0].message == "src/a.py ~ other:src/a.py"
    disjoint = gate(tmp_path, good_card(write_set=["src/z.py"]), key="me",
                    running_write_sets=running)
    assert disjoint.ready


def test_write_overlap_excludes_itself(tmp_path):
    running = {"me": ["src/a.py"]}
    assert gate(tmp_path, good_card(), key="me", running_write_sets=running).ready
    assert not gate(tmp_path, good_card(), key="you", running_write_sets=running).ready


def test_risk_tags_absent_vs_empty(tmp_path):
    card = good_card()
    del card["risk_tags"]
    assert gate(tmp_path, card).codes() == ["no_risk_tags"]
    assert gate(tmp_path, good_card(risk_tags=None)).codes() == ["no_risk_tags"]
    assert gate(tmp_path, good_card(risk_tags=[])).ready
    assert gate(tmp_path, good_card(risk_tags=["auth"])).ready


def test_no_window_class(tmp_path):
    assert gate(tmp_path, good_card(window_class=None)).codes() == ["no_window_class"]
    assert gate(tmp_path, good_card(window_class="2m")).codes() == ["no_window_class"]
    assert gate(tmp_path, good_card(window_class="1m")).ready


def test_estimate_missing(tmp_path):
    assert gate(tmp_path, good_card(estimate_tokens=None)).codes() == ["estimate_missing"]
    assert gate(tmp_path, good_card(estimate_tokens=0)).ready


@pytest.mark.parametrize("wc", ["128k", "256k", "1m"])
def test_estimate_boundary(tmp_path, wc):
    limit = int(USABLE_FRACTION * WINDOW_TOKENS[wc])
    assert gate(tmp_path, good_card(window_class=wc, estimate_tokens=limit)).ready
    v = gate(tmp_path, good_card(window_class=wc, estimate_tokens=limit + 1))
    assert v.codes() == ["estimate_too_big"]


def test_estimate_too_big_skipped_without_window(tmp_path):
    v = gate(tmp_path, good_card(window_class=None, estimate_tokens=10**9))
    assert v.codes() == ["no_window_class"]


def test_leg_request_too_big(tmp_path):
    card = good_card(estimate_tokens=5000)
    assert gate(tmp_path, card, leg_max_request=5000).ready
    assert gate(tmp_path, card, leg_max_request=4999).codes() == ["leg_request_too_big"]
    assert gate(tmp_path, card).ready
    both = gate(tmp_path, good_card(estimate_tokens=10**6), leg_max_request=10)
    assert both.codes() == ["estimate_too_big", "leg_request_too_big"]


def test_require_heldout(tmp_path):
    assert gate(tmp_path, good_card()).ready
    assert gate(tmp_path, good_card(), require_heldout=True).codes() == ["no_heldout"]
    assert gate(tmp_path, good_card(heldout_check="  "),
                require_heldout=True).codes() == ["no_heldout"]
    assert gate(tmp_path, good_card(heldout_check="true"), require_heldout=True).ready


def test_all_findings_collected_in_order(tmp_path):
    card = {"outcome": "x", "write_set": ["src/a.py", "/abs"], "read_set": ["gone"]}
    v = gate(tmp_path, card, key="me", running_write_sets={"o": ["src"]})
    assert v.codes() == ["bad_path", "no_check", "read_missing", "write_overlap",
                         "no_risk_tags", "no_window_class", "estimate_missing"]
    assert v.summary() == (
        "me: not ready - bad_path,no_check,read_missing,write_overlap,"
        "no_risk_tags,no_window_class,estimate_missing")
    assert isinstance(v.findings[0], Finding)


# ------------------------------------------------------------------ check_cards

def test_check_cards_sorted_and_peers_not_running(tmp_path):
    cards = {"b": good_card(), "a": good_card(), "c": good_card(write_set=["src/z.py"])}
    out = check_cards(cards, repo_root=tmp_path, dry_run=ok_run)
    assert list(out) == ["a", "b", "c"]
    assert all(v.ready for v in out.values())
    assert [v.key for v in out.values()] == ["a", "b", "c"]
    with_run = check_cards(cards, repo_root=tmp_path, dry_run=ok_run,
                           running_write_sets={"r": ["src/a.py"]})
    assert [k for k, v in with_run.items() if not v.ready] == ["a", "b"]
    assert os.path.isdir(tmp_path)
