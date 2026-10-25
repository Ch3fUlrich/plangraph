"""PG-EFFICIENCY-GATES (D-2739 §0): keeper, spawner, seat-parser and lane gates."""
import asyncio
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_src = str(ROOT / "src")
sys.path = [_src] + [p for p in sys.path if p != _src and not p.endswith("/tmp/spawn4a/src")]
for _m in list(sys.modules):
    if _m == "plangraph" or _m.startswith("plangraph."):
        del sys.modules[_m]

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
def test_spawn_limits_defaults_roles_and_env(monkeypatch):
    from plangraph.settings import SpawnLimits, spawn_limits
    for role in ("implement", "fix", "writer"):
        lim = spawn_limits(role)
        assert (lim.wall_cap_min, lim.max_cap_min) == (90, 180)
    for role in ("review", "seat"):
        lim = spawn_limits(role)
        assert (lim.wall_cap_min, lim.max_cap_min) == (45, 60)
    for role in ("design", "default", None):
        lim = spawn_limits(role)
        assert (lim.wall_cap_min, lim.max_cap_min) == (60, 120)
    assert lim.stall_min == 15 and lim.stall_cpu_pct == 2 and lim.floor_mb == 2000
    assert lim.max_runs == 4 and lim.require_principal is False
    assert lim.ignore == {}
    assert "opencode" in lim.truncation and "opencode" in lim.provider_protocol
    with pytest.raises(Exception):
        lim.wall_cap_min = 100
    monkeypatch.setenv("PLANGRAPH_SPAWN_LIMITS", json.dumps({"writer": {"wall_cap_min": 10}}))
    assert spawn_limits("implement").wall_cap_min == 10
    assert spawn_limits("implement").max_cap_min == 180


@pytest.mark.parametrize("override", [
    {"bad_role": {}}, {"writer": {"bad_key": 1}}, {"writer": {"wall_cap_min": -1}},
    {"writer": {"require_principal": "yes"}}, {"writer": {"ignore": "nope"}},
    {"writer": {"truncation": {"opencode": [123]}}},
])
def test_spawn_limits_refusals(monkeypatch, override):
    from plangraph.settings import SettingsError, spawn_limits
    monkeypatch.setenv("PLANGRAPH_SPAWN_LIMITS", json.dumps(override))
    with pytest.raises(SettingsError):
        spawn_limits("writer")


def test_liveness_exit_vocabulary():
    from plangraph.keeper.schema import EXIT_CLASSES
    from plangraph.spawner.liveness import LIVENESS_TO_EXIT_CLASS, LIVENESS_VALUES, exit_class_for_liveness
    assert set(LIVENESS_TO_EXIT_CLASS) == set(LIVENESS_VALUES)
    assert exit_class_for_liveness(None) is None
    for liv, ec in LIVENESS_TO_EXIT_CLASS.items():
        assert ec in EXIT_CLASSES, f"{ec} not in EXIT_CLASSES"
        assert exit_class_for_liveness(liv) == ec
    assert LIVENESS_TO_EXIT_CLASS["stalled"] == "stall"
    assert LIVENESS_TO_EXIT_CLASS["capped"] == "timeout"
    assert LIVENESS_TO_EXIT_CLASS["attempt_timeout"] == "timeout"
    assert LIVENESS_TO_EXIT_CLASS["truncated"] == "worker_crash"
    assert LIVENESS_TO_EXIT_CLASS["provider_protocol"] == "worker_crash"
    assert LIVENESS_TO_EXIT_CLASS["oom"] == "worker_crash"
    assert LIVENESS_TO_EXIT_CLASS["memguard"] == "outage"
    assert LIVENESS_TO_EXIT_CLASS["killed"] == "outage"
    assert LIVENESS_TO_EXIT_CLASS["host_full"] == "outage"


def test_run_result_refusals_and_findings(tmp_path):
    from plangraph.spawner.liveness import LIVENESS_VALUES
    pass_text = "F1 [HIGH] defect\nVERDICT: PASS\n"
    for liv in LIVENESS_VALUES:
        assert F.failure_reason(pass_text, liveness=liv) == f"run liveness {liv}"
        assert F.seat_outcome(pass_text, liveness=liv)[0] is None
        assert F.first_review({"a": (pass_text, {"liveness": liv}), "b": pass_text})[0] is None
    for kw, exp in [({"exit_class": "cancelled"}, "run cancelled"), ({"output_cut": True}, "output cut")]:
        assert F.failure_reason(pass_text, **kw) == exp
        assert F.seat_outcome(pass_text, **kw)[0] is None
        assert F.first_review({"a": (pass_text, kw), "b": pass_text})[0] is None

    run_dir, seat_file = tmp_path / "run-1", tmp_path / "run-1" / "seat.out"
    run_dir.mkdir(); seat_file.write_text(pass_text)
    for bad in ({"liveness": "stalled"}, {"exit_class": "cancelled"}, {"output_cut": True}, {"status": "failed"}):
        (run_dir / "result.json").write_text(json.dumps({"status": "completed", "exit_class": "ok", **bad}))
        assert F.findings_from_files([seat_file]) == []
    (run_dir / "result.json").write_text(json.dumps({"status": "completed", "exit_class": "ok"}))
    assert len(F.findings_from_files([seat_file])) == 1
    for junk in ("{not json", "null", "[]", '"junk"'):
        (run_dir / "result.json").write_text(junk)
        assert F.findings_from_files([seat_file]) == [], junk  # haiku-C2, qwen-C2: unreadable/non-dict result.json refuses the seat
    for meta in (None, [], "junk", 3):
        assert F.first_review({"a": (PASS_SEAT, meta), "b": PASS_SEAT})[0] is None  # haiku-C2, qwen-C1: no verdict from such a seat
    assert F.first_review({"a": (PASS_SEAT, {"status": "completed"}), "b": PASS_SEAT})[0] == "pass"
    (run_dir / "result.json").write_text(json.dumps({"status": "completed", "exit_class": "ok"}))
    with pytest.raises(OSError):
        F.findings_from_files([run_dir])  # qwen-C2: missing stdout.log refuses loudly, never skips
    (run_dir / "stdout.log").write_text(pass_text)
    assert len(F.findings_from_files([run_dir])) == 1

    standalone = tmp_path / "other.out"
    standalone.write_text(pass_text)
    for bad in ({"liveness": "stalled"}, {"output_cut": True}, {"exit_class": "cancelled"}, {"status": "failed"}):
        assert F.findings_from_files([standalone], card={"seat_classes": {"other": {"status": "completed", **bad}}}) == []
    assert len(F.findings_from_files([standalone], card={"seat_classes": {"other": {"status": "completed"}}})) == 1

    from plangraph.spawner.service import SpawnService
    serv = SpawnService(state=tmp_path)
    called = []
    serv._spawn = lambda **kw: called.append(kw)
    for bad in ({"liveness": "stalled"}, {"exit_class": "cancelled"}, {"output_cut": True}, {"status": "failed"}):
        asyncio.run(serv._continue_writer(dict(run_id="r", role="implement", exit_code=0, cwd=str(tmp_path), **bad), "Remaining gaps: x"))
    assert called == []

    from tools.lane_gates import seats
    s1, s2 = tmp_path / "s1", tmp_path / "s2"
    s1.mkdir(); s2.mkdir()
    for s in (s1, s2):
        (s / "stdout.log").write_text("VERDICT: PASS\n")
        (s / "result.json").write_text(json.dumps({"status": "completed"}))
    assert seats([str(s1), str(s2)]) == 0
    (s1 / "result.json").write_text(json.dumps({"status": "completed", "output_cut": True}))
    assert seats([str(s1), str(s2)]) == 3
    for junk in ("{not json", "null", "[]", '"junk"'):
        (s1 / "result.json").write_text(junk)
        assert seats([str(s1), str(s2)]) == 3, junk  # haiku-C1, qwen-C1: corrupt result.json never falls back to the raw reply
    (s1 / "result.json").write_text(json.dumps({"status": "completed"}))
    assert seats([str(s1), str(s2)]) == 0


def test_output_on_disk_under_and_over_2mb(tmp_path):
    from plangraph.spawner.service import SpawnService
    serv = SpawnService(state=tmp_path)
    rec1 = dict(run_id="run-u", status="completed", exit_code=0, stdout_tail="small out", stderr_tail="small err")
    serv._save(rec1)
    run_dir = tmp_path / "runs" / "run-u"
    assert (run_dir / "stdout.log").read_text() == "small out"
    assert json.loads((run_dir / "result.json").read_text())["output_cut"] is False
    assert rec1["stdout_path"] == str(run_dir / "stdout.log")

    big_text = "H" * 1048576 + "M" * 500000 + "T" * 1048576
    rec2 = dict(run_id="run-o", status="completed", exit_code=0, stdout_tail=big_text)
    serv._save(rec2)
    run_dir2 = tmp_path / "runs" / "run-o"
    assert rec2["output_cut"] is True and json.loads((run_dir2 / "result.json").read_text())["output_cut"] is True
    saved = (run_dir2 / "stdout.log").read_text()
    assert saved.startswith("H" * 1048576) and saved.endswith("T" * 1048576) and "[... output cut ...]" in saved
    assert len(rec2["stdout_tail"].encode()) <= 4096

    from types import SimpleNamespace
    serv.runner.jobs["j9"] = SimpleNamespace(stdout="ATTEMPT-TWO", stderr="e2")
    rec3 = dict(run_id="run-t", status="completed", exit_code=0, job_id="j9",
                stdout_tail="ATTEMPT-ONE", stderr_tail="e1")
    serv._save(rec3)
    log = tmp_path / "runs" / "run-t" / "stdout.log"
    assert log.read_text() == "ATTEMPT-ONEATTEMPT-TWO"  # haiku-C3, qwen-C5: every attempt's text, not just the live one
    serv.runner.jobs["jA"] = SimpleNamespace(stdout="ATTEMPT-THREE", stderr="e3")
    rec3["job_id"] = "jA"
    serv._save(rec3)
    assert log.read_text() == "ATTEMPT-ONEATTEMPT-TWOATTEMPT-THREE"  # a fall-through attempt appends
    serv.runner.jobs["jc"] = SimpleNamespace(stdout="A" * 1100000, stderr="")
    rec4 = dict(run_id="run-c", status="completed", exit_code=0, job_id="jc")
    serv._save(rec4)
    assert rec4["output_cut"] is False  # positive control: one attempt under 2 MB is kept whole
    serv.runner.jobs["jd"] = SimpleNamespace(stdout="B" * 1100000, stderr="")
    rec4["job_id"] = "jd"
    serv._save(rec4)
    assert rec4["output_cut"] is True  # I17: the cut measures the accumulated attempts
    cut_log = (tmp_path / "runs" / "run-c" / "stdout.log").read_text()
    serv.runner.jobs.clear()
    serv._save(serv.result("run-c"))  # job gone: the log is never rewritten from the 4 KB tail
    assert (tmp_path / "runs" / "run-c" / "stdout.log").read_text() == cut_log
    from plangraph.spawner.runner import spawn_service
    dsp = spawn_service(state=tmp_path / "dsp")
    dsp.runner.jobs["j9"] = SimpleNamespace(stdout="ATTEMPT-TWO", stderr="e2")
    dsp._save(dict(run_id="run-d", status="completed", exit_code=0, job_id="j9",
                   stdout_tail="ATTEMPT-ONE", stderr_tail="e1"))
    assert (tmp_path / "dsp" / "runs" / "run-d" / "stdout.log").read_text() == "ATTEMPT-ONEATTEMPT-TWO"  # qwen-C5: DispatchService._save must not clobber the tail from the live job


def test_cancel_with_class_racing_minus_9():
    from plangraph.spawner.runner import SpawnerRunner
    runner = SpawnerRunner()
    async def run():
        class FakeProc:
            pid, returncode = 999, -9
            async def communicate(self): return b"", b""
        proc = FakeProc()
        job = await runner.spawn("t", model_or_combo="omniroute/cx-gpt-6.1-sol", dry_run=True)
        job.status = "running"; runner.jobs[job.job_id] = job; runner._processes[job.job_id] = proc
        task = asyncio.create_task(runner._monitor_process(job.job_id, proc, "t"))
        runner._tasks[job.job_id] = task
        await runner.cancel(job.job_id, liveness="capped")
        await asyncio.gather(task, return_exceptions=True)
        assert (job.liveness, job.exit_class, job.status) == ("capped", "timeout", "failed")

        job2 = await runner.spawn("t2", model_or_combo="omniroute/cx-gpt-6.1-sol", dry_run=True)
        job2.status = "running"; runner.jobs[job2.job_id] = job2; runner._processes[job2.job_id] = proc
        task2 = asyncio.create_task(runner._monitor_process(job2.job_id, proc, "t2"))
        runner._tasks[job2.job_id] = task2
        await runner.cancel(job2.job_id)
        await asyncio.gather(task2, return_exceptions=True)
        assert (job2.liveness, job2.exit_class, job2.status) == (None, "cancelled", "cancelled")
    asyncio.run(run())


def test_attempt_classification_reset_and_plain_failures(tmp_path, monkeypatch):
    # haiku-C4: each attempt resets liveness/exit_class (the last attempt wins, never stale);
    # qwen-C3: both execute() paths classify every attempt, plain failures included (I13).
    from types import SimpleNamespace
    from plangraph.planner.legs import Leg
    from plangraph.planner.routing import SpawnPlan
    from plangraph.spawner.runner import SpawnerRunner, spawn_service
    from plangraph.spawner.service import SpawnService
    async def passthrough(plan, dry_run=False):  # plan_for_launch_async is awaited (runner.py)
        return plan
    monkeypatch.setattr("plangraph.spawner.resolver.plan_for_launch_async", passthrough)

    def leg(i):
        return Leg(f"leg{i}", "paid", "opencode", f"model{i}", "fam", (), 200000, True, roles=("implement",))

    def job(jid, status, liveness=None, exit_class=None):
        return SimpleNamespace(job_id=jid, status=status, liveness=liveness, exit_class=exit_class,
                               exit_code=0 if status in ("completed", "dry_run") else 1,
                               stdout="out", stderr="", served_model="m", served_family="f",
                               served_witness_reason=None, events=[], cwd=None)

    async def no_writer(*a, **k):
        pass

    async def drive(service, jobs, legs):
        service._route = lambda *a, **k: SpawnPlan(tuple(legs), (), "route", (), ())
        service._continue_writer = no_writer
        todo = iter(jobs)
        async def fake_spawn(**kw):
            j = next(todo)
            if j.liveness:
                service.runner.provider_responses[j.job_id] = {"stop": True}  # force fall-through to the next leg
            return j
        service.runner.spawn = fake_spawn
        return await service._spawn(card={"privacy": "private"}, prompt="t", role="implement", wait_seconds=None)

    makers = (
        ("SpawnService", lambda: SpawnService(runner=SpawnerRunner(default_cwd=str(tmp_path)), state=tmp_path / "s1")),
        ("DispatchService", lambda: spawn_service(runner=SpawnerRunner(default_cwd=str(tmp_path)), state=tmp_path / "s2")),
    )
    for name, make in makers:
        service = make()
        rec = asyncio.run(drive(service, [job("a1", "failed", "truncated", "worker_crash"), job("a2", "completed")], [leg(1), leg(2)]))
        assert (rec["liveness"], rec["exit_class"]) == (None, "ok"), name  # haiku-C4: a completed attempt resets, never stale 'truncated'
        assert rec["status"] == "completed" and len(rec["attempts"]) == 2, name
        rec2 = asyncio.run(drive(service, [job("b1", "failed")], [leg(3)]))
        assert (rec2["liveness"], rec2.get("exit_class")) == (None, "check_fail"), name  # qwen-C3: plain failures too, never null
        res = json.loads((Path(service.state) / "runs" / rec2["run_id"] / "result.json").read_text())
        assert res["exit_class"] == "check_fail", name  # qwen-C3: result.json on the CLI/MCP path carries the class


def test_cancel_liveness_validated():
    from plangraph.routing_errors import ParameterRefused
    from plangraph.spawner.runner import SpawnerRunner
    runner = SpawnerRunner()
    async def run():
        job = await runner.spawn("t", model_or_combo="omniroute/cx-gpt-6.1-sol", dry_run=True)
        job.status = "running"; runner.jobs[job.job_id] = job
        with pytest.raises(ParameterRefused):
            await runner.cancel(job.job_id, liveness="bogus")  # haiku-C8, qwen-C4: unknown liveness refused
        assert (job.liveness, job.exit_class) == (None, None)  # nothing classified outside EXIT_CLASSES (I13)
        await runner.cancel(job.job_id, liveness="capped")  # positive control: a known liveness is accepted
        assert (job.liveness, job.exit_class) == ("capped", "timeout")
    asyncio.run(run())


