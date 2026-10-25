"""PG-EFFICIENCY-GATES (D-2739 §0): keeper, spawner, seat-parser and lane gates."""
import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from keeper_util import World, defn, seed, to_claimed, to_in_review
from plangraph.gates import admission, findings as F, lane, models, rounds
from plangraph.gates.followup import followup_reason
from plangraph.keeper import fmt
from plangraph.keeper.errors import Refused
from plangraph.keeper.events import verify_integrity
from plangraph.keeper.stall import stall_alarms
from plangraph.review.verdict import judge
from plangraph.routing_errors import SpawnRefused

ROOT = Path(__file__).resolve().parent.parent
FIX = {"kind": "fix", "note": "seat round findings", "findings": ["F1", "F2"]}
DESIGN = {"kind": "design", "ref": "docs/design/X.md@abc", "author_model": "claude-opus-5-5",
          "reviewed_by": "deepseek-direct-flash run-1", "invariants": "| I1 | never fail open |"}


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    seed(world, [("A", []), ("B", [])])
    yield world
    world.close()


# ------------------------------------------------------------ §0.1 follow-ups
@pytest.mark.parametrize("ident", ["X-LOWS", "X-FIX", "X-FU", "X-FOLLOWUP", "X-SQ3", "X-R2", "X-FIX-2"])
def test_followup_suffix_refused_for_l1(w, ident):
    with pytest.raises(Refused, match="followup_item"):
        w.store.create_items(w.p("l1main"), [defn(ident, under="demo:GOAL")])


@pytest.mark.parametrize("ident", ["X-FIXTURES", "ROUTER-R", "X-PREFIX", "SQL-LOAD"])
def test_plain_ids_are_not_followups(ident):
    assert followup_reason({"id": ident, "project": "demo"}, {}) is None


def test_fixes_open_item_refused_done_item_allowed(w):
    with pytest.raises(Refused, match="followup_item: .*still open"):
        w.store.create_items(w.p("l1main"), [defn("NEW", fixes="demo:A", under="demo:GOAL")])
    items = {"demo:A": {"key": "demo:A", "state": "done"}}
    assert followup_reason({"id": "NEW", "project": "demo", "corrects": "A"}, items) is None


def test_router_and_system_may_create_followups(w):
    w.store.create_items(w.p("router"), [defn("A-FIX", under="demo:GOAL")])
    assert w.store.show(w.p("reader"), "demo:A-FIX")["state"] == "open"


# ------------------------------------------------------------ §0.1 fix log + §0.2e rounds
def test_fix_log_hands_in_review_back_and_counts_one_round(w):
    to_in_review(w, "demo:A")
    out = w.store.log_item(w.p("l1main"), "demo:A", FIX)
    assert out["state"] == "claimed" and out["rounds"] == 1
    info = w.store.show(w.p("reader"), "demo:A")
    assert info["state"] == "claimed"
    assert info["gates"]["fix_log"][0]["findings"] == ["F1", "F2"]
    text = fmt.show_text(info)
    assert "rounds 1 | since replan 1" in text and "log 1 fix" in text and "[F1,F2]" in text
    assert verify_integrity(w.conn) == []


def test_reviewer_change_then_fix_log_is_one_round(w):
    to_in_review(w, "demo:A")
    w.store.verdict(w.p("reviewer"), "demo:A", False, "HIGH finding")
    out = w.store.log_item(w.p("l1main"), "demo:A", FIX)
    assert out["rounds"] == 1


def test_fix_log_handback_does_not_count_as_reviewer_streak(w):
    to_in_review(w, "demo:A")
    w.store.log_item(w.p("l1main"), "demo:A", FIX)
    epoch = w.store.show(w.p("reader"), "demo:A")["epoch"]
    w.store.submit(w.p("l1main"), "demo:A", epoch)
    out = w.store.verdict(w.p("reviewer"), "demo:A", False, "still wrong")
    assert out["state"] == "claimed"  # first real change request, not the second


def test_round_cap_alarm_and_replan_reset(w):
    to_claimed(w, "demo:A")
    for _ in range(3):
        w.store.log_item(w.p("l1main"), "demo:A", FIX)
    kinds = [r["kind"] for r in stall_alarms(w.conn, w.clock(), git=lambda *a: [])
             if r["key"] == "demo:A"]
    assert "review_round_cap" in kinds
    with pytest.raises(Refused, match="strong author model"):
        w.store.log_item(w.p("l1main"), "demo:A", dict(DESIGN, kind="replan",
                                                       author_model="mimo/mimo-v2.6-flash"))
    out = w.store.log_item(w.p("l1main"), "demo:A", dict(DESIGN, kind="replan"))
    assert out["rounds"] == 3 and out["since_replan"] == 0
    kinds = [r["kind"] for r in stall_alarms(w.conn, w.clock(), git=lambda *a: [])
             if r["key"] == "demo:A"]
    assert "review_round_cap" not in kinds


def test_log_item_roles_and_validation(w):
    to_claimed(w, "demo:A")
    for who in ("workerA", "reviewer", "l1other"):
        with pytest.raises(Refused, match="forbidden"):
            w.store.log_item(w.p(who), "demo:A", FIX)
    for bad in ({"kind": "fix", "note": "x"}, {"kind": "nope"}, dict(FIX, extra=1),
                dict(FIX, findings=["x" * 65]), {"kind": "design", "author_model": "claude-opus-5-5"}):
        with pytest.raises(Refused, match="validation"):
            w.store.log_item(w.p("l1main"), "demo:A", bad)
    w.store.log_item(w.p("router"), "demo:A", FIX)


def test_log_item_refused_on_done_item(w):
    from keeper_util import to_done
    to_done(w, "demo:B")
    with pytest.raises(Refused, match="locked"):
        w.store.log_item(w.p("l1main"), "demo:B", FIX)


def test_count_rounds_pure():
    ev = [{"type": "changes_requested", "payload": {}},
          {"type": "item_logged", "payload": {"kind": "fix"}},
          {"type": "item_logged", "payload": {"kind": "fix"}},
          {"type": "item_logged", "payload": DESIGN},
          {"type": "changes_requested", "payload": {}}]
    s = rounds.count_rounds(ev)
    assert (s["rounds"], s["since_replan"], s["design"]["ref"]) == (3, 3, DESIGN["ref"])
    assert rounds.cap_reached(s)


# ------------------------------------------------------------ §0.2c verdict parser
SEAT_PASS_WITH_HIGH = """## Findings
- **HIGH** — src/x.py:f — fails open on empty input.
  Failing input: [].
- LOW — naming.

TEST-GAMING: no - searched tests: 0 hits
VERDICT: PASS
"""


@pytest.mark.parametrize("text,outcome", [
    (SEAT_PASS_WITH_HIGH, "changes"),
    ("- LOW: nit\nVERDICT: APPROVE", "pass"),
    ("HIGH: none\nMED: 0\nVERDICT: PASS", "pass"),
    ("| # | Finding | Status |\n| A-1 | HIGH — x | **FIXED** — ok |\nVERDICT: PASS", "pass"),
    ("| A-3 | MEDIUM — y | **PARTIALLY FIXED** |\nVERDICT: PASS", "changes"),
    ("- HIGH|MED|LOW: <finding>\nVERDICT: PASS", "pass"),
    ("VERDICT fail", "changes"),
    ("", None),
    ("Error: 402 Payment Required", None),
    ("I reviewed it, looks fine.", None),
    ('{"type":"result","is_error":true,"result":"VERDICT: PASS"}', None),
    ('{"type":"result","is_error":false,"result":"1. (MED) x\\nVERDICT: pass"}', "changes"),
])
def test_seat_outcome(text, outcome):
    assert F.seat_outcome(text)[0] == outcome


def test_judge_and_seat_verdict_override_pass_with_high(tmp_path):
    assert judge(SEAT_PASS_WITH_HIGH, "stop").verdict == "fix"
    seat = tmp_path / "seat"
    seat.mkdir()
    (seat / "review.txt").write_text(SEAT_PASS_WITH_HIGH)
    (seat / "evidence.json").write_text(json.dumps(
        {"served_model": "deepseek-v4.1-flash", "finish_reason": "stop", "test_gaming": "no"}))
    out = subprocess.run([sys.executable, str(ROOT / "tools/seat_verdict.py"), "--key", "demo:A",
                          "--seat-dir", str(seat), "--writer-family", "google",
                          "--token-dir", str(tmp_path), "--dry-run"],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["body"]["ok"] is False
    assert "overridden" in out.stderr


# ------------------------------------------------------------ §0.2d fix brief
def test_fix_brief_carries_every_finding_and_rules(tmp_path):
    seat = tmp_path / "ds.out"
    seat.write_text("F7 [HIGH] keeper fails open\n  detail line\n\nVERDICT: FAIL\n")
    found = F.findings_from_files([seat])
    assert [f["id"] for f in found] == ["F7"]
    brief = F.build_fix_brief(found, "| I1 | x |", note="L1: also see X")
    assert "F7" in brief and "detail line" in brief and "| I1 | x |" in brief
    assert "whole defect class" in brief and "FULL affected suite" in brief
    assert brief.index("F7") < brief.index("L1: also see X")
    assert F.missing_ids("only F1 here", found) == ["F7"]
    assert F.missing_ids("x F7 y", found) == []
    assert F.missing_ids("F70", found) == ["F7"]


# ------------------------------------------------------------ spawner admission
def _item(**kw):
    base = {"key": "demo:A", "review_tier": "standard", "gates": {}}
    base.update(kw)
    return base


def test_admit_refusals():
    card = {"key": "demo:A"}
    with pytest.raises(admission.GateRefused, match="base-pin"):
        admission.admit(card, "implement", "first git merge origin/main, then fix")
    with pytest.raises(admission.GateRefused, match="round-cap"):
        admission.admit(card, "implement", "", _item(gates={"since_replan": 3}))
    with pytest.raises(admission.GateRefused, match="plan-gate"):
        admission.admit(card, "implement", "", _item(review_tier="high"))
    with pytest.raises(admission.GateRefused, match="plan-gate"):
        admission.admit(dict(card, est_lines=900), "implement", "", _item())
    ok = _item(review_tier="high", gates={"design": DESIGN, "since_replan": 2})
    assert admission.admit(card, "implement", "write it", ok)[0] == "write it"
    # reviews and reads are never gated
    assert admission.admit(card, "review", "merge main", _item(gates={"since_replan": 9}))[0] == "merge main"
    assert isinstance(admission.GateRefused("x"), SpawnRefused)


def test_admit_builds_fix_brief_with_design_invariants():
    card = {"key": "demo:A", "findings": [{"id": "H1", "text": "x", "severity": "HIGH"}]}
    prompt, notes = admission.admit(card, "implement", "note", _item(gates={"design": DESIGN}))
    assert "H1" in prompt and "never fail open" in prompt and prompt.rstrip().endswith("note")
    assert notes == ["fix brief built by code: 1 finding(s)"]
    assert "gates_skipped" in admission.admit({"key": "demo:A"}, "implement", "", None)[1][0]


def test_fixer_floor_drops_flash_and_weaker_legs():
    legs = [{"model": "mimo/mimo-v2.6-flash"}, {"model": "mimo/mimo-v2.6-pro"},
            {"model": "agy/gemini-3.8-flash"}, {"model": "deepseek/deepseek-chat"}]
    kept, dropped = admission.filter_fixer_legs(legs, {"findings": ["x"]}, "implement")
    assert [l["model"] for l in kept] == ["mimo/mimo-v2.6-pro", "deepseek/deepseek-chat"]
    kept, _ = admission.filter_fixer_legs(legs, {"findings": ["x"], "writer_model": "claude-opus-5-5"},
                                          "implement")
    assert [l["model"] for l in kept] == ["mimo/mimo-v2.6-pro"]
    assert admission.filter_fixer_legs(legs, {}, "implement")[0] == legs  # plain writer
    assert models.model_tier("unknown-model") == models.STANDARD


def test_spawn_service_refuses_merge_main_and_records_refusal(tmp_path, monkeypatch):
    from plangraph.spawner.service import SpawnService
    monkeypatch.delenv("PG_URL", raising=False)
    service = SpawnService(runner=object(), state=tmp_path)
    assert service.item_reader is None
    with pytest.raises(admission.GateRefused):
        asyncio.run(service.spawn(card={"key": "demo:A"}, role="implement",
                                  prompt="git rebase origin/main first"))
    records = list((tmp_path / "runs").glob("*/record.json"))
    assert records and json.loads(records[0].read_text())["status"] == "refused"


def test_spawn_service_round_cap_via_item_reader(tmp_path, monkeypatch):
    from plangraph.spawner.service import SpawnService
    service = SpawnService(runner=object(), state=tmp_path)
    service.item_reader = lambda key: _item(gates={"since_replan": 3})
    with pytest.raises(admission.GateRefused, match="round-cap"):
        asyncio.run(service.spawn(card={"key": "demo:A"}, role="implement", prompt="go"))


def test_keeper_item_reader_unconfigured_and_fail_closed(tmp_path):
    from plangraph.spawner.notify import keeper_item_reader
    assert keeper_item_reader(url="", token_file="") is None
    token = tmp_path / "t"
    token.write_text("x")
    read = keeper_item_reader(url="http://127.0.0.1:9", token_file=str(token))
    with pytest.raises(SpawnRefused, match="keeper show demo:A failed"):
        read("demo:A")


@pytest.mark.parametrize("status,body,expect", [(404, b"", None), (200, b'{"key": "demo:A"}', "demo:A"),
                                                (200, b"{}", None), (500, b"", "refused")])
def test_keeper_item_reader_status(tmp_path, status, body, expect):
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from plangraph.spawner.notify import keeper_item_reader

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(status)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    token = tmp_path / "t"
    token.write_text("x")
    try:
        read = keeper_item_reader(url=f"http://127.0.0.1:{server.server_port}", token_file=str(token))
        if expect == "refused":
            with pytest.raises(SpawnRefused, match="HTTP 500"):
                read("demo:A")
        else:
            assert (read("demo:A") or {}).get("key") == expect
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


# ------------------------------------------------------------ §0.2b/f lane gates
def test_numstat_counting_and_slices():
    text = "10\t5\tsrc/a.py\n-\t-\timg.png\n300\t0\tuv.lock\n2\t2\tsrc/{old => new}/b.py\n"
    assert lane.count_numstat(text) == (20, 3, ["src/a.py", "img.png", "src/new/b.py"])
    assert lane.size_verdict(150, 3)[0] == "ok"
    assert lane.size_verdict(250, 3)[0] == "warn"
    assert lane.size_verdict(401, 3)[0] == "fail"
    assert lane.size_verdict(100, 16)[0] == "fail"
    assert lane.size_verdict(900, 30, slice_budget=1000)[0] == "ok"
    assert lane.size_verdict(1100, 30, slice_budget=1000)[0] == "fail"
    design = (ROOT / "docs/design/PG-EFFICIENCY-GATES.md").read_text()
    assert lane.design_slices(design) == {1: 3000}
    assert lane.parse_slice_trailer("docs/design/PG-EFFICIENCY-GATES.md#1") == (
        "docs/design/PG-EFFICIENCY-GATES.md", 1)


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _commit(repo, name, lines, msg="c"):
    (repo / name).write_text("x\n" * lines)
    _git(repo, "add", name)
    _git(repo, "commit", "-qm", msg)


def test_lane_gates_cli(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    _commit(repo, "base.txt", 1)
    _git(repo, "checkout", "-qb", "lane")
    _commit(repo, "a.py", 50)
    tool = [sys.executable, str(ROOT / "tools/lane_gates.py"), "check", "--repo", str(repo),
            "--base", "main", "--head", "lane"]
    assert subprocess.run(tool, capture_output=True).returncode == 0
    for n in range(2):  # two merges from main -> base pin fails
        _git(repo, "checkout", "-q", "main")
        _commit(repo, f"m{n}.txt", 1)
        _git(repo, "checkout", "-q", "lane")
        _git(repo, "merge", "-q", "--no-edit", "main")
    out = subprocess.run(tool, capture_output=True, text=True)
    assert out.returncode == 1 and "base FAIL: 2 merges" in out.stdout
    _git(repo, "checkout", "-q", "main")
    _git(repo, "checkout", "-qb", "big")
    _commit(repo, "big.py", 450)
    big = tool[:-1] + ["big"]
    out = subprocess.run(big, capture_output=True, text=True)
    assert out.returncode == 1 and "size FAIL" in out.stdout
    (repo / "docs").mkdir()
    (repo / "docs/D.md").write_text("## Slices\n\n| slice | scope | max_lines |\n|---|---|---|\n| 1 | all | 600 |\n")
    _git(repo, "add", "docs/D.md")
    _git(repo, "commit", "-qm", "design\n\nPlan-Slice: docs/D.md#1")
    out = subprocess.run(big, capture_output=True, text=True)
    assert out.returncode == 0, out.stdout
