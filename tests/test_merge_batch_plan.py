"""tools/merge_batch_plan.py and merge_batch_util.py: stack order, HIGH tier, caps, exclusions.

Pure functions, no git and no gh. Split out of test_merge_batch.py for the 400-line limit; the
plan CLI and the git/gh flows stay there.

Mutations killed: stacked PR before its base, HIGH and standard PRs sharing a batch or HIGH batches
ignoring a cap or splitting a stack, a stack above a HIGH PR or a file list gh cut short read as
safe, a draft without the label, a red-CI or needs-update PR batched, a hung command that hangs;
a missing gate file that still tiers PRs (with --high-path), a rename out of a HIGH path read as
standard, a build set that breaks the plan rules read as one batch.
"""
import json
import sys
from pathlib import Path

import pytest

from merge_batch_env import ROOT, pr_json

sys.path.insert(0, str(ROOT / "tools"))

import merge_batch_plan  # noqa: E402
from merge_batch_plan import high_patterns, plan_batches, set_problems, stack_parents  # noqa: E402
from merge_batch_util import MergeBatchError, norm_pr, run  # noqa: E402


def P(number, ref=None, **kw):
    return norm_pr(pr_json(number, ref or f"lane/p{number}", **kw))


def test_plan_stack_order():
    prs = [P(12, base="lane/p10"), P(11), P(10)]  # 12 is based on PR 10's branch
    plan = plan_batches(prs, "main")
    assert [r["number"] for r in plan["prs"]] == [10, 12, 11]
    assert plan["prs"][1]["after"] == 10 and plan["batches"][0]["prs"] == [10, 12, 11]
    assert stack_parents([P(1, ref="main", cross=True), P(2)]) == {}  # a fork's main is no stack parent
    loop = plan_batches([P(1, base="lane/p2"), P(2, base="lane/p1")], "main")
    assert [r["reason"] for r in loop["prs"]] == ["stack cycle", "stack cycle"]


def test_plan_high_split():
    prs = [P(1, files=["docs/a.md"]), P(2, files=["src/plangraph/keeper/store.py"]), P(3, files=["tools/x.py"]),
           P(4, base="lane/p2", files=["docs/b.md"]), P(5, files=[".github/workflows/tests.yml"]),
           P(6, files=["tools/ci_commit_gate.py"])]
    plan = plan_batches(prs, "main", patterns=high_patterns())
    assert [(b["tier"], b["prs"]) for b in plan["batches"]] == [("STANDARD", [1, 3]), ("HIGH", [2, 4, 5, 6])]
    by_num = {r["number"]: r for r in plan["prs"]}
    assert by_num[4]["tier"] == "HIGH" and by_num[4]["high"] is False  # stacked on a HIGH PR
    assert by_num[2]["high_paths"] == ["src/plangraph/keeper/store.py"] and by_num[1]["high_paths"] == []
    assert high_patterns(("docs/*",)) != high_patterns()
    cut = plan_batches([P(7, files=["docs/a.md"], changed=300)], "main", patterns=high_patterns())  # gh cut the list
    assert cut["batches"][0]["tier"] == "STANDARD" and cut["prs"][0]["high_paths"] == []


def test_plan_caps():
    prs = [P(n, lines=100) for n in range(1, 6)]
    assert [b["prs"] for b in plan_batches(prs, "main", max_prs=2)["batches"]] == [[1, 2], [3, 4], [5]]
    assert [b["prs"] for b in plan_batches(prs, "main", max_lines=250)["batches"]] == [[1, 2], [3, 4], [5]]
    prs = [P(1, lines=100), P(2, lines=100), P(3, base="lane/p2", lines=100), P(4, lines=900)]
    plan = plan_batches(prs, "main", max_prs=2, max_lines=500)  # the 2+3 stack is never split
    assert [b["prs"] for b in plan["batches"]] == [[1], [2, 3], [4]]
    assert [b["over_cap"] for b in plan["batches"]] == [False, False, True]


def test_plan_high_batches_obey_the_caps():
    hi = "src/plangraph/keeper/store.py"
    prs = [P(1, files=[hi], lines=100), P(2, files=[hi], lines=100), P(3, files=[hi], lines=100),
           P(4, base="lane/p3", lines=100), P(5, lines=100), P(6, lines=100), P(7, files=[hi], lines=700)]
    plan = plan_batches(prs, "main", max_prs=2, max_lines=800, patterns=high_patterns())
    # HIGH units: 1, 2, {3,4} (a stack, never split), 7; standard units: 5, 6
    assert [(b["tier"], b["prs"]) for b in plan["batches"]] == [
        ("HIGH", [1, 2]), ("HIGH", [3, 4]), ("STANDARD", [5, 6]), ("HIGH", [7])]
    assert [b["over_cap"] for b in plan["batches"]] == [False, False, False, False]
    wide = plan_batches(prs, "main", max_lines=250, patterns=high_patterns())  # lines cap: 7 (700) is alone
    assert [(b["tier"], b["prs"], b["over_cap"]) for b in wide["batches"]] == [
        ("HIGH", [1, 2], False), ("HIGH", [3, 4], False), ("STANDARD", [5, 6], False), ("HIGH", [7], True)]


def test_plan_excludes():
    prs = [P(1, draft=True, labels=()), P(2, ci="red"), P(3, labels=("batch-ready", "needs-update")),
           P(4, base="release"), P(5, base="lane/p1"), P(6, ci="pending"), P(7, ci="none"), P(8, state="CLOSED"),
           P(9, draft=True), P(10, draft=True, ci="red"), P(11, draft=True, labels=("batch-ready", "needs-update"))]
    plan = plan_batches(prs, "main")
    why = {r["number"]: r["reason"] for r in plan["prs"] if r["reason"]}
    assert why == {1: "draft", 2: "ci red", 3: "label needs-update", 4: "targets 'release', not 'main'",
                   5: "stacked on #1 (draft)", 8: "not open (closed)", 10: "ci red", 11: "label needs-update"}
    # PR 9 is a draft, but it carries batch-ready: the label overrides draft status
    assert [b["prs"] for b in plan["batches"]] == [[6, 7, 9]]


def test_a_hung_command_is_an_error_not_a_hang():
    with pytest.raises(MergeBatchError, match="did not finish in 1s"):
        run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=1)
    with pytest.raises(MergeBatchError, match="cannot run"):
        run(["/nonexistent/merge-batch-tool"])


def test_pr_head_must_be_a_sha():
    with pytest.raises(MergeBatchError, match="not a commit sha"):
        norm_pr(pr_json(1, "lane/p1", head="--upload-pack=x"))


def test_high_patterns_fail_closed_without_the_gate_lists(monkeypatch):
    """The import of tools/ci_commit_gate.py failed: --high-path adds globs, it cannot replace the AUTH_DOOR list."""
    assert any(pat.startswith("tools/ci_commit_gate.py") for pat in high_patterns(("docs/*",)))
    monkeypatch.setattr(merge_batch_plan, "AUTH_DOOR_PATHS", ())
    for extra in ((), ("docs/*",)):
        with pytest.raises(MergeBatchError, match="HIGH path list is unknown"):
            high_patterns(extra)


def test_plan_high_by_the_old_path_of_a_rename():
    moved = P(1, files=["docs/store.py"])  # gh lists only the new path of src/plangraph/keeper/store.py
    assert plan_batches([moved], "main", patterns=high_patterns())["prs"][0]["tier"] == "STANDARD"
    moved["extra"] = ["src/plangraph/keeper/store.py"]  # what git diff --no-renames adds
    row = plan_batches([moved], "main", patterns=high_patterns())["prs"][0]
    assert row["tier"] == "HIGH" and row["high_paths"] == ["src/plangraph/keeper/store.py"]


def test_set_problems_name_what_breaks_the_plan_rules():
    hi = "src/plangraph/keeper/store.py"
    assert set_problems(plan_batches([P(1), P(2)], "main", patterns=high_patterns())) == []
    red = set_problems(plan_batches([P(1), P(2, ci="red"), P(3, labels=("batch-ready", "needs-update"))], "main"))
    assert red == ["#2 ci red", "#3 label needs-update"]
    mixed = set_problems(plan_batches([P(1, files=[hi]), P(2)], "main", patterns=high_patterns()))
    assert len(mixed) == 1 and "HIGH #1" in mixed[0] and "STANDARD #2" in mixed[0] and "reviewed apart" in mixed[0]
    cut = set_problems(plan_batches([P(1), P(2), P(3)], "main", max_prs=2))
    assert len(cut) == 1 and "cuts them into 2 batches" in cut[0]
    big = set_problems(plan_batches([P(1, lines=2000)], "main"))
    assert big == ["1 PR(s) and 2000 lines are over the caps (--max-prs 6, --max-lines 1200)"]



def test_deploy_blocker_items_are_filed_one_at_a_time_and_a_retry_is_idempotent(monkeypatch, tmp_path, capsys):
    """The filing path of the go-req deploy blockers: one define per item, an existing blocker never rejects a new
    one, a refusal names its item, an unconfigured keeper skips."""
    import types

    items = tmp_path / "blockers.json"
    items.write_text(json.dumps({"items": [{"key": "plangraph:OLD", "id": "OLD"}, {"key": "plangraph:NEW", "id": "NEW"}]}))
    calls, answers = [], {}

    def fake_cli(*args):
        sent = json.loads(Path(args[args.index("--file") + 1]).read_text())["items"]
        calls.append([item["key"] for item in sent])
        rc, err = answers.get(sent[0]["key"], (0, ""))
        return types.SimpleNamespace(returncode=rc, stderr=err, stdout="")

    monkeypatch.setattr(merge_batch_plan, "keeper_cli", fake_cli)
    monkeypatch.setattr(merge_batch_plan, "keeper_landing_configured", lambda: True)
    merge_batch_plan.file_deploy_blockers(items)
    assert calls == [["plangraph:OLD"], ["plangraph:NEW"]] and capsys.readouterr().err == ""
    calls.clear()
    answers["plangraph:OLD"] = (1, "refused: conflict: plangraph:OLD already exists\n")  # the retry of a GO-REQ
    merge_batch_plan.file_deploy_blockers(items)
    assert calls == [["plangraph:OLD"], ["plangraph:NEW"]] and capsys.readouterr().err == ""
    answers["plangraph:OLD"] = (1, "refused: keeper unavailable\n")  # any other refusal is reported, not fatal
    merge_batch_plan.file_deploy_blockers(items)
    assert "deploy blocker item plangraph:OLD not filed: refused: keeper unavailable" in capsys.readouterr().err
    assert calls[-1] == ["plangraph:NEW"]
    monkeypatch.setattr(merge_batch_plan, "keeper_landing_configured", lambda: False)
    calls.clear()
    merge_batch_plan.file_deploy_blockers(items)
    assert calls == [] and "keeper is not configured" in capsys.readouterr().err


def test_declared_hosts_come_from_the_deploy_hosts_lines_of_the_active_items(monkeypatch):
    import types

    shown = {"p:A": {"description": "Why: x\nDeploy-Hosts: alpha, beta"}, "p:B": {"description": "no hosts"}}

    def fake_cli(verb, key, flag):
        assert (verb, flag) == ("show", "--json")
        if key == "p:GONE":
            return types.SimpleNamespace(returncode=1, stdout="", stderr="not found")
        return types.SimpleNamespace(returncode=0, stdout=json.dumps(shown[key]), stderr="")

    active = {"merged": [{"items": ["p:A", "p:B"]}, {"items": ["p:A", "p:GONE"]}]}
    monkeypatch.setattr(merge_batch_plan, "keeper_cli", fake_cli)
    monkeypatch.setattr(merge_batch_plan, "keeper_landing_configured", lambda: True)
    assert merge_batch_plan.declared_hosts(active) == {"p:A": ["alpha", "beta"]}
    monkeypatch.setattr(merge_batch_plan, "keeper_landing_configured", lambda: False)
    assert merge_batch_plan.declared_hosts(active) == {}
