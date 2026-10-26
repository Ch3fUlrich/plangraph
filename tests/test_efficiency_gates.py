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
    ("insufficient balance, add credits\nVERDICT: PASS", None),
    ("rate limit hit, partial review\n**VERDICT**: PASS", None),
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
    found = F.findings_from_files([seat], card={"seat_classes": {"ds": {"status": "completed"}}})  # qwen-E2/haiku-E1: the file form needs seat_classes or a sibling result.json
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
    assert lane.design_slices(design) == {1: 3000, 2: 600, 3: 450, 4: 400, 5: 300, 6: 400, 7: 400, 8: 400}
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


# ------------------------------------------------------------ D-2744 retired family, first-review seats
def test_retired_family_dropped_from_every_role(tmp_path):
    from plangraph.planner.legs import DEFAULT_LEGS
    from plangraph.planner.routing import SpawnPlan
    from plangraph.spawner.service import SpawnService
    assert models.retired("omniroute/deepseek-direct-flash") == "deepseek"
    assert models.retired("x/y", family="DeepSeek") == "deepseek"
    assert models.retired("mimo/mimo-v2.6-pro", family="mimo") is None
    service = SpawnService(runner=object(), state=tmp_path)
    legs = tuple(leg for leg in DEFAULT_LEGS if leg.family in ("deepseek", "mimo"))
    for role in ("review", "implement"):
        plan = service._gate_plan(SpawnPlan(legs, (), "t", (), ()), {}, role)
        assert plan.legs and all(leg.family != "deepseek" for leg in plan.legs)
        assert {s["reason"] for s in plan.skipped} == {"retired_family"}
    only = service._gate_plan(SpawnPlan(tuple(l for l in legs if l.family == "deepseek"), (), "t", (), ()),
                              {}, "review")
    assert not only.legs and "D-2744" in only.error


PASS_SEAT = "looks fine\nVERDICT: PASS\n"
FIX_SEAT = "- HIGH src/a.py:3 wrong sign\nVERDICT: PASS\n"


@pytest.mark.parametrize("outputs,expect", [
    ({"mimo": PASS_SEAT, "qwen": PASS_SEAT}, "pass"),
    ({"mimo": PASS_SEAT, "qwen": FIX_SEAT}, "changes"),
    ({"mimo": PASS_SEAT, "qwen": ""}, None),
    ({"mimo": FIX_SEAT, "qwen": "Error: 402 insufficient balance"}, None),
    ({"mimo": PASS_SEAT}, None),
])
def test_first_review_needs_two_seats_with_text(outputs, expect):
    outcome, reasons = F.first_review(outputs)
    assert outcome == expect, reasons


def test_lane_gates_seats_cli(tmp_path):
    (tmp_path / "result.json").write_text(json.dumps({"status": "completed"}))  # qwen-E2/haiku-E1: the file form is verified by a sibling result.json
    (tmp_path / "mimo.out").write_text(PASS_SEAT)
    (tmp_path / "qwen.out").write_text("")
    tool = [sys.executable, str(ROOT / "tools/lane_gates.py"), "seats",
            str(tmp_path / "mimo.out"), str(tmp_path / "qwen.out")]
    out = subprocess.run(tool, capture_output=True, text=True)
    assert out.returncode == 3 and "no verdict" in out.stdout
    (tmp_path / "qwen.out").write_text(PASS_SEAT)
    assert subprocess.run(tool, capture_output=True).returncode == 0


def test_no_bare_python_invocation():
    """D-2746 (11): hosts ship ``python3`` only; code, tests, scripts and docs never call ``python``."""
    import re
    bare = re.compile(r"(^|[^\w/.-])python(\s+-m\s|\s+tools/|\s+tests/)|^#!/usr/bin/env python$", re.M)
    files = subprocess.run(["git", "-C", str(ROOT), "ls-files", "src", "tests", "tools", "skills", "docs",
                            ".github", "deploy", "README.md", "CONTRIBUTING.md"],
                           capture_output=True, text=True, check=True).stdout.split()
    skip = ("deploy/bootstrap.ps1", "docs/plan/")  # Windows ships ``python``; plan docs are history
    hits = []
    for name in files:
        if name.startswith(skip) or name == "tests/test_efficiency_gates.py":
            continue
        try:
            text = (ROOT / name).read_text("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        hits += [f"{name}: {m.group(0).strip()}" for m in bare.finditer(text)]
    assert hits == []


def test_no_import_time_sys_mutation():
    """haiku-C10/qwen-C7: no module-level interpreter-state rewrite; plangraph comes from this checkout."""
    here = (ROOT / "tests" / "test_efficiency_gates.py").read_text("utf-8")
    assert ("sys." + "path") not in here and ("sys." + "modules") not in here
    import plangraph
    assert str(Path(plangraph.__file__).resolve()).startswith(str(ROOT / "src"))


def _sensitive_world(tmp_path, monkeypatch, private=True):
    """A git run tree + a repo registry marking ``demo`` private (or public)."""
    from plangraph.gates import privacy
    tree = tmp_path / "tree"
    tree.mkdir()
    subprocess.run(["git", "init", "-q", str(tree)], check=True)
    (tree / "a.py").write_text("x = 1\n")
    registry = tmp_path / "repos.json"
    registry.write_text(json.dumps({"repos": {"demo": {"private": private}}}))
    monkeypatch.setenv(privacy.REGISTRY_ENV, str(registry))
    return tree


SENSITIVE = {"estimate_tokens": 20000, "privacy": "sensitive", "key": "demo:X", "paths": ["src/a.py"]}


@pytest.mark.parametrize("path, secret", [
    ("vault/prod-api-key.json", True), ("config/.env", True), (".env.local", True),
    ("keys/server.pem", True), ("x/id_rsa", True), ("db.kdbx", True), ("ci/service-account-ci.json", True),
    ("secrets/x.txt", True), ("a/credentials.json", True), ("api_keys.yml", True), ("app.p8", True),
    ("store.jks", True), ("C:\\fleet\\vault\\x.json", True), ("tools/my_apikey.txt", True),
    ("src/a.py", False), ("docs/keys.md", False), ("environment.py", False), ("pemberton.txt", False),
    ("src/envelope.py", False), ("keyboard.py", False),
])
def test_secret_path_matcher(path, secret):
    # D-2760 F1: one matcher, wider set, near-misses stay clean.
    from plangraph.gates.privacy import secret_path
    assert secret_path(path) is secret


@pytest.mark.parametrize("role", ["implement", "review"])
def test_sensitive_card_reaches_only_marked_models(tmp_path, monkeypatch, role):
    # D-2683 / D-2760 F4: the route marker keeps a sensitive card on gemini (agy) / mimo only.
    from plangraph.gates.privacy import sensitive_leg_ok
    from plangraph.planner import routing
    _sensitive_world(tmp_path, monkeypatch)
    plan = routing.plan_spawn(dict(SENSITIVE), role=role)
    assert plan.legs and all(sensitive_leg_ok(leg) for leg in plan.legs)
    assert {leg.id for leg in plan.legs} <= {"agy-gemini-3.8-flash", "mimo-v2.6-flash", "mimo-v2.6-pro"}
    assert not all(sensitive_leg_ok(leg) for leg in routing.plan_spawn({"estimate_tokens": 20000}, role=role).legs)
    with pytest.raises(ValueError):
        routing.plan_spawn({"estimate_tokens": 20000, "privacy": "secret"}, role=role)


@pytest.mark.parametrize("change, why", [
    ({"paths": ["vault/prod-api-key.json"]}, "secret path"),
    ({"risk_tags": ["secrets"]}, "risk tag secrets"),
    ({"paths": "src/a.py"}, "unknown scope|must be a list"),
    ({"paths": None}, "unknown scope"),
    ({"key": "other:X"}, "registry marks private"),
    ({"key": None, "repo": None}, "unknown repo"),
])
def test_sensitive_card_refusals(tmp_path, monkeypatch, change, why):
    # D-2683 / D-2738 / D-2760 F2/F3: refuse, naming D-2683, failing closed.
    from plangraph.planner import routing
    _sensitive_world(tmp_path, monkeypatch)
    card = {k: v for k, v in {**SENSITIVE, **change}.items() if v is not None}
    with pytest.raises(SpawnRefused, match=why) as err:
        routing.plan_spawn(card, role="implement")
    assert "D-2683" in str(err.value)


def test_sensitive_card_public_or_missing_registry_refused(tmp_path, monkeypatch):
    from plangraph.gates import privacy
    from plangraph.planner import routing
    _sensitive_world(tmp_path, monkeypatch, private=False)
    with pytest.raises(SpawnRefused, match="D-2683"):
        routing.plan_spawn(dict(SENSITIVE), role="implement")
    monkeypatch.setenv(privacy.REGISTRY_ENV, str(tmp_path / "absent.json"))
    with pytest.raises(SpawnRefused, match="D-2683"):
        routing.plan_spawn(dict(SENSITIVE), role="implement")


def test_sensitive_spawn_end_to_end(tmp_path, monkeypatch):
    # D-2760 F2/F5: admit + refuse through SpawnService (route, override, run tree).
    from plangraph.spawner.runner import SpawnerRunner
    from plangraph.spawner.service import SpawnService
    tree = _sensitive_world(tmp_path, monkeypatch)
    service = SpawnService(runner=SpawnerRunner(default_cwd=str(tree)), state=tmp_path / "state")

    async def run():
        record = await service.spawn(card=dict(SENSITIVE), prompt="t", dry_run=True)
        assert record["plan"]["first_leg"]["id"] in ("agy-gemini-3.8-flash", "mimo-v2.6-pro", "mimo-v2.6-flash")
        with pytest.raises(SpawnRefused, match="marked model"):
            await service.spawn(card=dict(SENSITIVE), prompt="t", dry_run=True,
                                leg="cx-gpt-6.1-sol", override_reason="test override leg")
        (tree / ".env").write_text("A=1\n")
        with pytest.raises(SpawnRefused, match="run tree"):
            await service.spawn(card=dict(SENSITIVE), prompt="t", dry_run=True)
        (tree / ".env").unlink()
        (tree / "vault").mkdir()
        (tree / "vault" / "x.json").write_text("{}")
        with pytest.raises(SpawnRefused, match="run tree"):
            await service.spawn(card=dict(SENSITIVE), prompt="t", dry_run=True)
    asyncio.run(run())


def test_sensitive_run_tree_not_git_fails_closed(tmp_path):
    from plangraph.gates.privacy import admit_run_tree
    with pytest.raises(SpawnRefused, match="not a readable git tree"):
        admit_run_tree(dict(SENSITIVE), tmp_path)
    admit_run_tree({"privacy": "private"}, tmp_path)


def test_opencode_command_runs_standalone():
    # D-2749: without --standalone `opencode run` attaches to the background
    # service, whose config has no mimo baseURL.
    from plangraph.routing_support.clients import build_opencode_command
    assert build_opencode_command("task", "mimo/mimo-v2.6-pro")[:3] == ["opencode", "run", "--standalone"]


DECISIONS_TEXT = """# DECISIONS

Preamble line.

## 0. Pipeline
0.2a **Plan gate.** first, D-1. `[keep]`
0.2b **Slice.** after 0.2a, Opus 5.5 only (D-2, D-3). `[gate:x.py]`

## 1. Other
1.1 **Cite.** see 0.2b. `[gate-todo]`
"""


def _decision_rows(w):
    from importlib import util
    spec = util.spec_from_file_location("dview", Path(__file__).resolve().parents[1] / "tools" / "decisions_view.py")
    mod = util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.export_items(w.store.snapshot(w.p("reader")))["items"]


def test_decisions_import_round_trip_and_tree(w):
    # D-2746 (10): rules live in the keeper; DECISIONS.md is their render.
    from plangraph.decisions_view import parse, render, to_rules, tree
    rules = to_rules(parse(DECISIONS_TEXT))
    assert render(rules) == DECISIONS_TEXT
    by_id = {r["id"]: r for r in rules}
    assert by_id["RULE-0-2B"]["depends_on"] == ["RULE-0-2A"]   # "Opus 5.5" is no rule ref
    assert by_id["RULE-1-1"]["depends_on"] == ["RULE-0-2B"]
    assert by_id["RULE-0-2B"]["note"] == "tag=gate:x.py; d=D-2,D-3"
    out = w.store.rule_set(w.p("router"), rules)
    assert (len(out["created"]), out["updated"]) == (6, [])
    rows = _decision_rows(w)
    assert render(rows) == DECISIONS_TEXT
    assert tree(rows, "RULE-1-1") == ["RULE-1-1", "RULE-0-2B", "RULE-0-2A"]
    # retire a rule: it drops out of the view; the event log still folds.
    out = w.store.rule_set(w.p("router"), [dict(by_id["RULE-1-1"], state="retired")])
    assert out == {"created": [], "updated": ["decisions:RULE-1-1"]}
    assert "1.1 " not in render(_decision_rows(w))
    with pytest.raises(Refused, match="retired"):
        w.store.rule_set(w.p("router"), [by_id["RULE-1-1"]])
    assert verify_integrity(w.conn) == []


@pytest.mark.parametrize("bad, code", [
    ([{"id": "RULE-1-1", "description": "x", "depends_on": ["RULE-9-9"]}], "unknown"),
    ([{"id": "RULE-1-1", "description": "x", "depends_on": ["RULE-1-2"]},
      {"id": "RULE-1-2", "description": "y", "depends_on": ["RULE-1-1"]}], "cycle"),
    ([{"id": "nope", "description": "x"}], "id"),
    ([], "rules"),
])
def test_rule_set_refusals(w, bad, code):
    with pytest.raises(Refused) as err:
        w.store.rule_set(w.p("router"), bad)
    assert code in str(err.value).lower()


def test_rule_set_router_only(w):
    with pytest.raises(Refused):
        w.store.rule_set(w.p("l1main"), [{"id": "RULE-1-1", "description": "x"}])


def test_design_role_takes_strongest_leg_first():
    # D-2754: designs / replans run through the spawner; D-2758 (5): Claude legs allowed.
    from plangraph.planner import routing
    from plangraph.planner.legs import DESIGN_ORDER
    plan = routing.plan_spawn({"estimate_tokens": 20000}, role="design")
    assert tuple(leg.id for leg in plan.legs) == DESIGN_ORDER
    assert any(leg.claude_family for leg in plan.legs)


def test_client_binary_by_absolute_path(tmp_path, monkeypatch):
    # D-2755: a spawned child without the login PATH still finds its client.
    from plangraph.routing_support import clients
    bin_dir = tmp_path / "node" / "v24" / "bin"
    bin_dir.mkdir(parents=True)
    exe = bin_dir / "opencode"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    env = {"PATH": str(tmp_path / "empty"), clients.CLIENT_BINS_ENV: str(tmp_path / "none.json")}
    assert clients.client_binary("opencode", env) is None
    monkeypatch.setattr(clients, "CLIENT_BIN_DIRS", (str(tmp_path / "node" / "*" / "bin"),))
    assert clients.client_binary("opencode", env) == str(exe)
    monkeypatch.setattr(clients, "CLIENT_BIN_DIRS", ())
    (tmp_path / "bins.json").write_text(json.dumps({"opencode": str(exe)}))
    env[clients.CLIENT_BINS_ENV] = str(tmp_path / "bins.json")
    assert clients.client_binary("opencode", env) == str(exe)


# ------------------------------------------------------------ Slice 4a liveness plumbing
_BAD_META = ({"liveness": "stalled"}, {"exit_class": "cancelled"}, {"output_cut": True}, {"status": "failed"})


def test_spawn_limits_defaults_roles_and_env(tmp_path, monkeypatch):
    from plangraph.settings import spawn_limits
    for roles, caps in ((("implement", "fix", "writer"), (90, 180)), (("review",), (45, 60)), (("design", "seat", "default", None), (60, 120))):  # design role map: writer = implement/fix/writer, seat = review, default = every other role
        for role in roles: lim = spawn_limits(role); assert (lim.wall_cap_min, lim.max_cap_min) == caps; assert (lim.stall_min, lim.stall_cpu_pct, lim.floor_mb, lim.max_runs, lim.require_principal, lim.ignore) == (15, 2, 2000, 4, False, {}); assert "opencode" in lim.truncation and "opencode" in lim.provider_protocol  # qwen-C9: defaults on every role, not only the default one
    with pytest.raises(Exception): spawn_limits(None).wall_cap_min = 100
    monkeypatch.setenv("PLANGRAPH_SPAWN_LIMITS", json.dumps({"writer": {"wall_cap_min": 10}})); assert (spawn_limits("implement").wall_cap_min, spawn_limits("implement").max_cap_min) == (10, 180)
    monkeypatch.delenv("PLANGRAPH_SPAWN_LIMITS"); monkeypatch.setenv("PLANGRAPH_SETTINGS", "policy.json")
    (tmp_path / "policy.json").write_text(json.dumps({"spawn_limits": {"seat": {"stall_min": 30}}})); assert spawn_limits("review", root=tmp_path).stall_min == 30 and spawn_limits("design", root=tmp_path).stall_min == 15 and spawn_limits("writer", root=tmp_path).stall_min == 15  # positive control (qwen-D7): a valid cross-bucket override is accepted on every request and only its own bucket feels it


# haiku-D6/qwen-D10: restored as its own parametrized test — 497257e3 folded it into a loop inside
# test_spawn_limits_defaults_roles_and_env (the name is part of "no test deleted", and a loop stops at
# the first bad shape instead of reporting each).
@pytest.mark.parametrize("override", [
    {"bad_role": {}}, {"writer": {"bad_key": 1}}, {"writer": {"wall_cap_min": -1}},
    {"writer": {"require_principal": "yes"}}, {"writer": {"ignore": "nope"}},
    {"writer": {"truncation": {"opencode": [123]}}},
    # qwen-D7: bad values in a bucket the call does not select are refused too.
    {"seat": {"wall_cap_min": -1}}, {"default": {"require_principal": "yes"}},
])
def test_spawn_limits_refusals(tmp_path, monkeypatch, override):
    from plangraph.settings import SettingsError, spawn_limits
    monkeypatch.setenv("PLANGRAPH_SPAWN_LIMITS", json.dumps(override))
    for role in ("implement", "review", "design"):  # qwen-D7: every section/env bucket is validated, not only the one the request selects
        with pytest.raises(SettingsError):
            spawn_limits(role)
    monkeypatch.delenv("PLANGRAPH_SPAWN_LIMITS"); monkeypatch.setenv("PLANGRAPH_SETTINGS", "policy.json")  # qwen-C9: the settings-file section refuses the same shapes (I1)
    (tmp_path / "policy.json").write_text(json.dumps({"spawn_limits": override}))
    for role in ("implement", "review", "design"):
        with pytest.raises(SettingsError):
            spawn_limits(role, root=tmp_path)


def test_liveness_exit_vocabulary():
    from plangraph.keeper.schema import EXIT_CLASSES
    from plangraph.spawner.liveness import LIVENESS_TO_EXIT_CLASS as M, LIVENESS_VALUES, exit_class_for_liveness as ecfl
    assert set(M) == set(LIVENESS_VALUES) and ecfl(None) is None
    for liv, ec in M.items(): assert ec in EXIT_CLASSES and ecfl(liv) == ec
    with pytest.raises(ValueError): ecfl("bogus")  # unknown liveness must refuse, never silent None (I13)
    assert M == {"truncated": "worker_crash", "provider_protocol": "worker_crash", "stalled": "stall", "capped": "timeout",
                 "attempt_timeout": "timeout", "oom": "worker_crash", "memguard": "outage", "killed": "outage", "host_full": "outage"}


def test_run_result_refusals_and_findings(tmp_path):
    from plangraph.spawner.liveness import LIVENESS_VALUES
    pass_text = "F1 [HIGH] defect\nVERDICT: PASS\n"
    for liv in LIVENESS_VALUES: assert F.failure_reason(pass_text, liveness=liv) == f"run liveness {liv}"; assert F.seat_outcome(pass_text, liveness=liv)[0] is None; assert F.first_review({"a": (pass_text, {"liveness": liv}), "b": pass_text})[0] is None
    for kw, exp in [({"exit_class": "cancelled"}, "run cancelled"), ({"output_cut": True}, "output cut")]: assert F.failure_reason(pass_text, **kw) == exp; assert F.seat_outcome(pass_text, **kw)[0] is None; assert F.first_review({"a": (pass_text, kw), "b": pass_text})[0] is None
    run_dir, seat_file = tmp_path / "run-1", tmp_path / "run-1" / "seat.out"; run_dir.mkdir(); seat_file.write_text(pass_text)
    for bad in _BAD_META: (run_dir / "result.json").write_text(json.dumps({"status": "completed", "exit_class": "ok", **bad})); assert F.findings_from_files([seat_file]) == []
    (run_dir / "result.json").write_text(json.dumps({"status": "completed", "exit_class": "ok"})); assert len(F.findings_from_files([seat_file])) == 1
    for junk in ("{not json", "null", "[]", '"junk"'): (run_dir / "result.json").write_text(junk); assert F.findings_from_files([seat_file]) == [], junk  # haiku-C2, qwen-C2: unreadable/non-dict result.json refuses the seat
    for meta in (None, [], "junk", 3): assert F.first_review({"a": (PASS_SEAT, meta), "b": PASS_SEAT})[0] is None  # haiku-C2, qwen-C1: no verdict from such a seat
    assert F.first_review({"a": (PASS_SEAT, {"status": "completed"}), "b": PASS_SEAT})[0] == "pass"
    (run_dir / "result.json").write_text(json.dumps({"status": "completed", "exit_class": "ok"}))
    with pytest.raises(OSError): F.findings_from_files([run_dir])  # qwen-C2: missing stdout.log refuses loudly, never skips
    (run_dir / "stdout.log").write_text(pass_text); assert len(F.findings_from_files([run_dir])) == 1
    standalone = tmp_path / "other.out"; standalone.write_text(pass_text)
    for bad in _BAD_META: assert F.findings_from_files([standalone], card={"seat_classes": {"other": {"status": "completed", **bad}}}) == []
    assert len(F.findings_from_files([standalone], card={"seat_classes": {"other": {"status": "completed"}}})) == 1
    from plangraph.spawner.service import SpawnService; from types import SimpleNamespace; from plangraph.routing_support.fixer import render_fixer_brief
    serv = SpawnService(state=tmp_path); called = []
    async def fake_spawn(**kw):  # haiku-C6/qwen-C6: _continue_writer calls runner.spawn, not self._spawn
        called.append(kw); return SimpleNamespace(job_id="j1", status="completed", exit_code=0, stdout="")
    serv.runner.spawn = fake_spawn
    # haiku-D3/qwen-D6: the refused records are spawn-shaped (chosen_leg included) and their output has gaps, so deleting the I13 guard sends every shape into the spawn call — the control fails on the guard itself, never on a KeyError or the unrelated reason table.
    reasons = []
    for bad in _BAD_META:
        rec = dict(run_id="run-r", role="implement", exit_code=0, cwd=str(tmp_path), chosen_leg={"model": "m", "harness": "h"}, **bad)
        asyncio.run(serv._continue_writer(rec, "Remaining gaps: x"))
        reasons.append(rec.get("continuation_reason"))
    assert reasons == ["liveness", "cancelled", "output_cut", "nonzero_exit"]  # haiku-D8/qwen-D11: every refusal cause names why, an output_cut record with exit 0 included
    assert called == []  # a refused record must never reach the spawn call site
    good = dict(run_id="run-r2", role="implement", exit_code=0, cwd=str(tmp_path), status="completed", chosen_leg={"model": "m", "harness": "h"})
    asyncio.run(serv._continue_writer(good, "Remaining gaps: x"))
    assert [kw["model_or_combo"] for kw in called] == ["m"]  # positive control: a good record DOES spawn the continuation
    assert called[0]["prompt"] == render_fixer_brief([{"text": "x"}])  # with the fixer brief built from the gaps
    assert good["continuation_reason"] == "remaining_gaps" and good["continuation_run_id"]
    from tools.lane_gates import seats
    s1, s2 = tmp_path / "s1", tmp_path / "s2"; s1.mkdir(); s2.mkdir()
    for s in (s1, s2): (s / "stdout.log").write_text("VERDICT: PASS\n"); (s / "result.json").write_text(json.dumps({"status": "completed"}))
    assert seats([str(s1), str(s2)]) == 0
    (s1 / "result.json").write_text(json.dumps({"status": "completed", "output_cut": True})); assert seats([str(s1), str(s2)]) == 3
    for junk in ("{not json", "null", "[]", '"junk"'): (s1 / "result.json").write_text(junk); assert seats([str(s1), str(s2)]) == 3, junk  # haiku-C1, qwen-C1: corrupt result.json never falls back to the raw reply
    (s1 / "result.json").write_text(json.dumps({"status": "completed"})); assert seats([str(s1), str(s2)]) == 0


def test_output_on_disk_under_and_over_2mb(tmp_path):
    from types import SimpleNamespace; from plangraph.spawner.service import SpawnService
    serv = SpawnService(state=tmp_path)
    kb = "".join("step %04d: ok\n" % i for i in range(200))  # haiku-C7: the under-cap case is a real few KB of harness-like output, not a few bytes
    rec1 = dict(run_id="run-u", status="completed", exit_code=0, stdout_tail=kb, stderr_tail="small err"); serv._save(rec1)
    run_dir = tmp_path / "runs" / "run-u"
    assert (run_dir / "stdout.log").read_text() == kb and rec1["stdout_tail"] == kb
    assert json.loads((run_dir / "result.json").read_text())["output_cut"] is False and rec1["stdout_path"] == str(run_dir / "stdout.log")
    big_text = "H" * 1048576 + "M" * 500000 + "T" * 1048576
    rec2 = dict(run_id="run-o", status="completed", exit_code=0, stdout_tail=big_text); serv._save(rec2)
    run_dir2 = tmp_path / "runs" / "run-o"
    assert rec2["output_cut"] is True and json.loads((run_dir2 / "result.json").read_text())["output_cut"] is True
    saved = (run_dir2 / "stdout.log").read_text(); assert saved.startswith("H" * 1048576) and saved.endswith("T" * 1048576) and "[... output cut ...]" in saved
    assert len(rec2["stdout_tail"].encode()) <= 4096
    serv.runner.jobs["j9"] = SimpleNamespace(stdout="ATTEMPT-TWO", stderr="e2")
    rec3 = dict(run_id="run-t", status="completed", exit_code=0, job_id="j9", stdout_tail="ATTEMPT-ONE", stderr_tail="e1"); serv._save(rec3)
    log = tmp_path / "runs" / "run-t" / "stdout.log"; assert log.read_text() == "ATTEMPT-ONEATTEMPT-TWO"  # haiku-C3, qwen-C5: every attempt's text, not just the live one
    serv.runner.jobs["jA"] = SimpleNamespace(stdout="ATTEMPT-THREE", stderr="e3"); rec3["job_id"] = "jA"
    serv._save(rec3); assert log.read_text() == "ATTEMPT-ONEATTEMPT-TWOATTEMPT-THREE"  # a fall-through attempt appends
    serv.runner.jobs["jc"] = SimpleNamespace(stdout="A" * 1100000, stderr="")
    rec4 = dict(run_id="run-c", status="completed", exit_code=0, job_id="jc"); serv._save(rec4); assert rec4["output_cut"] is False  # positive control: one attempt under 2 MB is kept whole
    serv.runner.jobs["jd"] = SimpleNamespace(stdout="B" * 1100000, stderr=""); rec4["job_id"] = "jd"
    serv._save(rec4); assert rec4["output_cut"] is True  # I17: the cut measures the accumulated attempts
    cut_log = (tmp_path / "runs" / "run-c" / "stdout.log").read_text(); serv.runner.jobs.clear()
    serv._save(serv.result("run-c")); assert (tmp_path / "runs" / "run-c" / "stdout.log").read_text() == cut_log  # job gone: the log is never rewritten from the 4 KB tail
    from plangraph.spawner.runner import spawn_service
    dsp = spawn_service(state=tmp_path / "dsp"); dsp.runner.jobs["j9"] = SimpleNamespace(stdout="ATTEMPT-TWO", stderr="e2")
    dsp._save(dict(run_id="run-d", status="completed", exit_code=0, job_id="j9", stdout_tail="ATTEMPT-ONE", stderr_tail="e1"))
    assert (tmp_path / "dsp" / "runs" / "run-d" / "stdout.log").read_text() == "ATTEMPT-ONEATTEMPT-TWO"  # qwen-C5: DispatchService._save must not clobber the tail from the live job


def test_wait_json_returns_the_paths(tmp_path):
    # haiku-C9/qwen-C8: the design's output-on-disk row says "`wait --json` returns the paths"; cli.py accepted --json on wait and never read it; a record stored before this slice returned no paths at all.
    import contextlib, io, time
    from plangraph.spawner.service import SpawnService; from plangraph.spawner.cli import main
    serv = SpawnService(state=tmp_path)
    serv._save(dict(run_id="run-w1", status="completed", exit_code=0, stdout="out", stderr="e", ended=time.time()))
    old = tmp_path / "runs" / "run-w2"; old.mkdir(parents=True)  # pre-slice record: no path fields
    (old / "record.json").write_text(json.dumps({"run_id": "run-w2", "status": "completed", "exit_code": 0, "ended": 1.0}))
    for run_id, run_dir in (("run-w1", tmp_path / "runs" / "run-w1"), ("run-w2", old)):
        cap = io.StringIO()
        with contextlib.redirect_stdout(cap): assert main(["--state", str(tmp_path), "wait", run_id, "--json"]) == 0
        out = json.loads(cap.getvalue()); assert out["stdout_path"] == out["stdout_log"] == str(run_dir / "stdout.log"); assert out["stderr_path"] == out["stderr_log"] == str(run_dir / "stderr.log")


def test_wait_plain_form_hides_the_paths(tmp_path):
    # haiku-E2 / qwen-E3: test_wait_json_returns_the_paths only proved the paths come back
    # with --json; deleting `if args.json:` (cli.py) and filling the paths in for every wait
    # kept it green. The plain form must NOT get the paths; the flag turns them on (control).
    import contextlib, io
    from plangraph.spawner.cli import main
    old = tmp_path / "runs" / "run-w3"; old.mkdir(parents=True)  # pre-slice record: no path fields
    (old / "record.json").write_text(json.dumps({"run_id": "run-w3", "status": "completed", "exit_code": 0, "ended": 1.0}))
    cap = io.StringIO()
    with contextlib.redirect_stdout(cap): assert main(["--state", str(tmp_path), "wait", "run-w3"]) == 0
    out = json.loads(cap.getvalue())
    assert "stdout_path" not in out and "stderr_path" not in out and "stdout_log" not in out
    cap = io.StringIO()  # positive control: the same record WITH --json gets them
    with contextlib.redirect_stdout(cap): assert main(["--state", str(tmp_path), "wait", "run-w3", "--json"]) == 0
    out = json.loads(cap.getvalue())
    assert out["stdout_path"] == out["stdout_log"] == str(old / "stdout.log")


def test_cancel_with_class_racing_minus_9():
    # haiku-C5: the monitor must actually run before the assertions read the job — the synchronous tree_killer never suspends, so task.cancel() landed on a monitor that had taken no step yet and every assertion held from cancel() alone. A killer that yields once hands the monitor its step; `ran` proves it took it. haiku-D4/qwen-D4: `ran` and the proc live per case, so the first case's step can never vouch for the second's, and the exit_code the monitor wrote (proc.returncode, never cancel()'s) shows its classification ran rather than holding from cancel() alone.
    from plangraph.spawner.runner import SpawnerRunner
    async def run():
        class FakeProc:
            pid, returncode = 999, -9
            def __init__(self, ran): self.ran = ran
            async def communicate(self): self.ran.append(1); return b"", b""
        async def yielding_killer(pid): await asyncio.sleep(0)
        runner = SpawnerRunner(tree_killer=yielding_killer)
        async def case(prompt, liv, expected):
            ran = []
            proc = FakeProc(ran)
            j = await runner.spawn(prompt, model_or_combo="omniroute/cx-gpt-6.1-sol", dry_run=True); j.status = "running"; runner.jobs[j.job_id] = j; runner._processes[j.job_id] = proc
            t = asyncio.create_task(runner._monitor_process(j.job_id, proc, prompt)); runner._tasks[j.job_id] = t
            await runner.cancel(j.job_id, liveness=liv); await asyncio.gather(t, return_exceptions=True)
            assert ran, "the monitor never ran; the classification would hold from cancel() alone"; assert (j.liveness, j.exit_class, j.status) == expected
            assert j.exit_code == proc.returncode  # the monitor's own path wrote the exit code
        await case("t", "capped", ("capped", "timeout", "failed")); await case("t2", None, (None, "cancelled", "cancelled"))
    asyncio.run(run())


def test_attempt_classification_reset_and_plain_failures(tmp_path, monkeypatch):
    # haiku-C4: each attempt resets liveness/exit_class (the last attempt wins, never stale); qwen-C3: both execute() paths classify every attempt, plain failures included (I13).
    from types import SimpleNamespace; from plangraph.planner.legs import Leg; from plangraph.planner.routing import SpawnPlan
    from plangraph.spawner.runner import SpawnerRunner, spawn_service; from plangraph.spawner.service import SpawnService
    async def passthrough(plan, dry_run=False):  # plan_for_launch_async is awaited (runner.py)
        return plan
    monkeypatch.setattr("plangraph.spawner.resolver.plan_for_launch_async", passthrough)
    def job(jid, status, liveness=None, exit_class=None):
        return SimpleNamespace(job_id=jid, status=status, liveness=liveness, exit_class=exit_class, exit_code=0 if status in ("completed", "dry_run") else 1,
                               stdout="out", stderr="", served_model="m", served_family="f", served_witness_reason=None, events=[], cwd=None)
    async def drive(service, jobs, legs):
        service._route = lambda *a, **k: SpawnPlan(tuple(legs), (), "route", (), ()); service._continue_writer = lambda *a, **k: asyncio.sleep(0)
        todo = iter(jobs)
        async def fake_spawn(**kw):
            j = next(todo)
            if j.liveness: service.runner.provider_responses[j.job_id] = {"stop": True}  # force fall-through to the next leg
            return j
        service.runner.spawn = fake_spawn; return await service._spawn(card={"privacy": "private"}, prompt="t", role="implement", wait_seconds=None)
    leg = lambda i: Leg(f"leg{i}", "paid", "opencode", f"model{i}", "fam", (), 200000, True, roles=("implement",))
    for name, make in (("SpawnService", lambda: SpawnService(runner=SpawnerRunner(default_cwd=str(tmp_path)), state=tmp_path / "s1")),
                       ("DispatchService", lambda: spawn_service(runner=SpawnerRunner(default_cwd=str(tmp_path)), state=tmp_path / "s2"))):
        service = make(); rec = asyncio.run(drive(service, [job("a1", "failed", "truncated", "worker_crash"), job("a2", "completed")], [leg(1), leg(2)]))
        assert (rec["liveness"], rec["exit_class"]) == (None, "ok"), name  # haiku-C4: a completed attempt resets, never stale 'truncated'
        assert rec["status"] == "completed" and len(rec["attempts"]) == 2, name
        rec2 = asyncio.run(drive(service, [job("b1", "failed", exit_class="worker_crash")], [leg(3)])); assert (rec2["liveness"], rec2.get("exit_class")) == (None, "worker_crash"), name  # qwen-D8: the monitor's class for the attempt (runner.py nonzero exit -> worker_crash) wins, never check_fail
        res = json.loads((Path(service.state) / "runs" / rec2["run_id"] / "result.json").read_text()); assert res["exit_class"] == "worker_crash", name  # qwen-D8: result.json on the CLI/MCP path carries the class
        rec3 = asyncio.run(drive(service, [job("b3", "failed")], [leg(4)])); assert rec3.get("exit_class") == "check_fail", name  # no monitor class at all: the status fallback still applies


def test_cancel_liveness_validated():
    from plangraph.routing_errors import ParameterRefused; from plangraph.spawner.runner import SpawnerRunner
    runner = SpawnerRunner()
    async def run():
        job = await runner.spawn("t", model_or_combo="omniroute/cx-gpt-6.1-sol", dry_run=True); job.status = "running"; runner.jobs[job.job_id] = job
        with pytest.raises(ParameterRefused): await runner.cancel(job.job_id, liveness="bogus")  # haiku-C8, qwen-C4: unknown liveness refused
        assert (job.liveness, job.exit_class) == (None, None)  # nothing classified outside EXIT_CLASSES (I13)
        await runner.cancel(job.job_id, liveness="capped"); assert (job.liveness, job.exit_class) == ("capped", "timeout")  # positive control
    asyncio.run(run())



# ------------------------------------------------------------ round 2a: missing result.json fails closed
def test_run_dir_without_result_json_refuses_the_seat(tmp_path):
    # haiku-D1, haiku-D2, qwen-D3 (class: a missing result.json fails open).
    # _save writes stdout.log before result.json, so a run dir with only
    # stdout.log is reachable; both seat paths must refuse it like the
    # missing-stdout case, never count the raw reply as a verdict.
    from tools.lane_gates import seats
    pass_text = "F1 [LOW] nit\nVERDICT: PASS\n"
    s1, s2 = tmp_path / "run-1", tmp_path / "run-2"
    for s in (s1, s2):
        s.mkdir()
        (s / "stdout.log").write_text(pass_text)
    assert seats([str(s1), str(s2)]) == 2  # refused loudly, never "first review PASS"
    with pytest.raises(OSError):
        F.findings_from_files([s1])  # admission path refuses loudly like the missing stdout.log
    for s in (s1, s2):  # positive control: the same run dirs with result.json are accepted
        (s / "result.json").write_text(json.dumps({"status": "completed", "exit_class": "ok"}))
    assert seats([str(s1), str(s2)]) == 0
    assert len(F.findings_from_files([s1])) == 1


# ------------------------------------------------------------ round 3: the file form fails closed too
def test_seat_file_without_result_json_fails_closed(tmp_path):
    # qwen-E2 / haiku-E1 (I16): the missing-result.json fail-closed fix covered only run dirs
    # (p.is_dir()); the file form the design documents (seats <files>, admission's seat_outputs)
    # fell to meta = None and was counted from the raw reply. A seat file whose run has no
    # result.json and no seat_classes entry is refused exactly like a corrupt result.json.
    from tools.lane_gates import seats
    pass_text = "F1 [LOW] nit\nVERDICT: PASS\n"
    a, b = tmp_path / "a.out", tmp_path / "b.out"
    a.write_text(pass_text); b.write_text(pass_text)
    assert F.findings_from_files([a]) == []  # refused, never counted from the raw reply
    assert seats([str(a), str(b)]) == 3  # no verdict, re-run — the haiku-D1 symptom dies here too
    (tmp_path / "result.json").write_text(json.dumps({"status": "completed", "exit_class": "ok"}))
    assert len(F.findings_from_files([a])) == 1 and seats([str(a), str(b)]) == 0  # positive control: a valid result.json accepts the same file
    (tmp_path / "result.json").unlink()
    assert len(F.findings_from_files([a], card={"seat_classes": {"a": {"status": "completed"}}})) == 1  # the design's file-form source accepts it
    (tmp_path / "result.json").write_text("{not json")
    assert F.findings_from_files([a]) == [] and seats([str(a), str(b)]) == 3  # corrupt refuses the same way, as before


# ------------------------------------------------------------ round 2a: a cold _save rebuilds the accumulation
def test_cold_save_rebuilds_output_accumulation(tmp_path):
    # qwen-D1 (I17): plangraph-spawn cancel (cli.py) and the watchdog's
    # spawn_service(state=...).retry_completions() (stall_watchdog.py:245) both build a fresh
    # service and re-save a record read back from disk. That cold _save must rebuild the
    # accumulation from the log on disk (the full text) — never rewrite the log from the
    # record's 4 KB display tail. The warm re-save is the positive control: the same record
    # through a service that still holds _accs keeps the log whole either way.
    import contextlib, io
    from plangraph.spawner.service import SpawnService
    from plangraph.spawner.cli import main
    from plangraph.spawner.runner import spawn_service
    big = "step ok\n" * 3000  # > 4 KB: the display tail alone cannot hold it
    serv = SpawnService(state=tmp_path)
    serv._save(dict(run_id="run-c1", status="running", exit_code=None, stdout_tail=big,
                    stderr_tail="e", started=1.0, ended=None, attempts=[]))
    log = tmp_path / "runs" / "run-c1" / "stdout.log"
    assert log.read_text() == big  # control: the warm save wrote the whole text
    serv._save(serv.result("run-c1"))
    assert log.read_text() == big  # control: a warm re-save keeps it (the _accs path)
    with contextlib.redirect_stdout(io.StringIO()):
        assert main(["--state", str(tmp_path), "cancel", "run-c1"]) == 1  # a cancelled run exits 1
    assert log.read_text() == big  # the cancel path's fresh service rebuilt from the log, not the tail
    serv._save(dict(run_id="run-c2", status="completed", exit_code=0, stdout_tail=big,
                    stderr_tail="e", started=1.0, ended=2.0, attempts=[]))
    log2 = tmp_path / "runs" / "run-c2" / "stdout.log"
    assert log2.read_text() == big
    spawn_service(state=tmp_path).retry_completions()  # the watchdog's cold re-save (run-c2 is pending)
    assert log2.read_text() == big
    assert (tmp_path / "runs" / "run-c1" / "stdout.log").read_text() == big  # and its re-save of run-c1
    # pre-slice records carry no logs: the attempt tails (each its job's full text) rebuild it
    run3 = tmp_path / "runs" / "run-c3"
    run3.mkdir(parents=True)
    (run3 / "record.json").write_text(json.dumps({"run_id": "run-c3", "status": "completed", "exit_code": 0,
        "ended": 1.0, "stdout_tail": "bbb", "stderr_tail": "",
        "attempts": [{"leg": {"id": "l1"}, "status": "completed", "stdout_tail": "ONE" + "a" * 5000, "stderr_tail": "e"},
                     {"leg": {"id": "l2"}, "status": "completed", "stdout_tail": "TWO" + "b" * 5000, "stderr_tail": "e"}]}))
    cold = SpawnService(state=tmp_path)
    cold._save(cold.result("run-c3"))
    assert (run3 / "stdout.log").read_text() == "ONE" + "a" * 5000 + "TWO" + "b" * 5000  # every attempt, not the tail


def test_output_cut_saved_before_continue_writer(tmp_path, monkeypatch):
    # qwen-D2: dispatch execute() (runner.py) must _save — which computes output_cut from the
    # accumulated logs (I17) — BEFORE _continue_writer reads it, mirroring the base ordering
    # (service.py: save, then continue). Otherwise a run whose output crossed 2 MB mid-job
    # continues the writer with output_cut still stale-false.
    from dataclasses import dataclass, asdict
    from types import SimpleNamespace
    import asyncio as aio
    import plangraph.gates.privacy as privacy
    import plangraph.settings as settings
    monkeypatch.setattr(privacy, 'admit_run_tree', lambda *a, **k: None)
    monkeypatch.setattr(settings, 'admit_review_scope', lambda *a, **k: None)
    monkeypatch.delenv('PLANGRAPH_SPAWN_NOTIFY_CMD', raising=False)

    @dataclass
    class FakeLeg:
        id: str = 'l1'; model: str = 'm'; harness: str = 'h'; family: str = 'fam'
    class FakePlan:
        legs = [FakeLeg()]; skipped = []; error = None; first_leg = legs[0]
        def to_dict(self): return {'skipped': [], 'legs': [asdict(self.legs[0])]}

    def drive(svc, output):
        called = []
        state = {'n': 0}
        async def fake_spawn(**kw):
            called.append(kw); state['n'] += 1
            job = SimpleNamespace(job_id=f'j{state["n"]}', status='completed', exit_code=0,
                stdout='', stderr='', liveness=None, cwd=None, served_model=None,
                served_family=None, served_witness_reason=None)
            # the first job's output only appears after the mid-flight save (as in a real job)
            out = output if state['n'] == 1 else ''
            async def fill(): job.stdout = out
            svc.runner._tasks[job.job_id] = fill()
            svc.runner.jobs[job.job_id] = job
            return job
        svc.runner.spawn = fake_spawn
        svc._route = lambda *a, **k: FakePlan()
        return called

    big = "Remaining gaps: x\n" + "z" * 2097152  # crosses the 2 MB cut with a gap that would continue
    small = "Remaining gaps: x\n"

    from plangraph.spawner.service import SpawnService
    base = SpawnService(state=tmp_path / 'base')
    called = drive(base, big)
    aio.run(base._spawn(card={'title': 't'}, prompt='p', role='implement', wait_seconds=None))
    assert len(called) == 1  # positive control: the base ordering (save, then continue) never continued

    from plangraph.spawner.runner import spawn_service
    dsp = spawn_service(state=tmp_path / 'dsp')
    called = drive(dsp, small)  # control: under the cut, with gaps, the dispatch path DOES continue
    aio.run(dsp._spawn(card={'title': 't'}, prompt='p', role='implement', wait_seconds=None))
    assert len(called) == 2

    dsp2 = spawn_service(state=tmp_path / 'dsp2')
    called = drive(dsp2, big)
    aio.run(dsp2._spawn(card={'title': 't'}, prompt='p', role='implement', wait_seconds=None))
    assert len(called) == 1  # output_cut must have stopped the continuation


def test_accs_pruned_when_record_ends(tmp_path):
    # haiku-D7, qwen-D9: a long-lived supervisor must drop the per-run accumulation (and task
    # handle) when the record ends — the same end-of-run pruning _child_envs already gets.
    from types import SimpleNamespace
    from plangraph.spawner.service import SpawnService
    serv = SpawnService(state=tmp_path)
    big = "step ok\n" * 3000
    rec = dict(run_id="run-p1", status="completed", exit_code=0, job_id="jp", started=1.0, ended=None, attempts=[])
    serv._save(rec)
    serv.runner.jobs["jp"] = SimpleNamespace(stdout=big, stderr="")
    serv.tasks["run-p1"] = object()  # the scheduled execute() task, keyed like _spawn does
    serv._save(rec)
    assert any(k[0] == "run-p1" for k in serv._accs)  # control: held while the run is open
    rec["ended"] = 2.0
    serv._save(rec)
    assert not any(k[0] == "run-p1" for k in serv._accs)  # the full output must not live on in memory
    assert "run-p1" not in serv.tasks
    log = tmp_path / "runs" / "run-p1" / "stdout.log"
    assert log.read_text() == big  # control: the log on disk is whole
    serv._save(serv.result("run-p1"))  # a later re-save shares the pruning and never shrinks the log
    assert log.read_text() == big
    assert not any(k[0] == "run-p1" for k in serv._accs)


# ------------------------------------------------------------ round 3: a post-prune terminal re-save counts the resident job once
def test_terminal_re_save_keeps_the_resident_jobs_text_once(tmp_path):
    # qwen-E1 (I17): DispatchService._save saves a terminal record twice (runner.py:567, :574)
    # and the end-of-run _accs prune makes the second save cold, where the attempt rows already
    # hold the resident job's full text (stdout_tail=job.stdout). Counting those tails in done
    # AND the live job's text in cur wrote the last attempt twice: the doubled log never trips
    # the on-disk-wins test against its own doubled size, and any run above ~1 MB crossed the
    # 2 MB cut -> output_cut, which refuses the run as a result (I13). The attempt for the
    # current job counts once, as cur. The same shape occurs on SpawnService.cancel after
    # execute's finally already saved (a cold re-save of a loaded record) — covered below.
    from types import SimpleNamespace
    from plangraph.spawner.runner import spawn_service
    dsp = spawn_service(state=tmp_path)
    text = "P" * 1100000  # > 1 MB: doubled it crosses the 2 MB cut and would set output_cut
    dsp.runner.jobs["j1"] = SimpleNamespace(stdout=text, stderr="e")
    rec = dict(run_id="run-e1", status="completed", exit_code=0, job_id="j1", started=1.0, ended=2.0,
               attempts=[dict(leg={"id": "l1"}, status="completed", job_id="j1",
                              stdout_tail=text, stderr_tail="e")])
    dsp._save(rec)  # terminal: DispatchService._save itself saves twice, the second one cold
    log = tmp_path / "runs" / "run-e1" / "stdout.log"
    assert log.read_text() == text  # exactly once, never text + text
    res = json.loads((tmp_path / "runs" / "run-e1" / "result.json").read_text())
    assert rec["output_cut"] is False and res["output_cut"] is False
    dsp._save(dsp.result("run-e1"))  # every later terminal save (cancel/watchdog shape) is cold too
    assert log.read_text() == text
    assert json.loads((tmp_path / "runs" / "run-e1" / "result.json").read_text())["output_cut"] is False
    # positive control: the multi-attempt cold rebuild still keeps every attempt's text once
    run2 = tmp_path / "runs" / "run-e2"; run2.mkdir(parents=True)
    (run2 / "record.json").write_text(json.dumps({"run_id": "run-e2", "status": "completed", "exit_code": 0,
        "ended": 1.0, "job_id": "j2",
        "attempts": [{"leg": {"id": "l1"}, "status": "completed", "job_id": "j1", "stdout_tail": "ONE", "stderr_tail": ""},
                     {"leg": {"id": "l2"}, "status": "completed", "job_id": "j2", "stdout_tail": "TWO", "stderr_tail": ""}]}))
    dsp.runner.jobs["j2"] = SimpleNamespace(stdout="TWO", stderr="")
    dsp._save(dsp.result("run-e2"))
    assert (run2 / "stdout.log").read_text() == "ONETWO"  # done + cur, each job once
