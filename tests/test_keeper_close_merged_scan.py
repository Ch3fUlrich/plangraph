"""Close-merged scan cost: one fetch and one document read per request phase."""
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from plangraph.keeper.errors import Refused
from test_keeper_close_merged import HEAD, MERGE, unseated_w  # noqa: F401

HEADER = ("| ID | PROJECT | DECISION | BY | DATE |\n| --- | --- | --- | --- | --- |\n")


class ScanGit:
    """A git double that counts fetches and answers a fixed head list."""

    def __init__(self, heads):
        self.heads = heads
        self.fetches = 0

    def repo_path(self, project):
        return "fixture-repo"

    def repo_ref(self, project):
        return "origin/main"

    def merge_commit_of(self, repo, sha, ref):
        return (sha, "fixture")

    def _run(self, repo, args, **kwargs):
        if args[0] == "fetch":
            self.fetches += 1
            return SimpleNamespace(returncode=0, stdout="")
        if "--format=%cI" in args:
            return SimpleNamespace(returncode=0, stdout="2026-11-12T12:00:00Z")
        return SimpleNamespace(returncode=0, stdout=f"{'2' * 40} {args[-1]}")


def go_row(did, head, date="2026-11-12", key="demo:H"):
    suffix = f" | ROUTER | {date} |" if date else " | ROUTER |"
    return f"| {did} | demo | GO #{did[2:]} {key}; GO-HEAD {head}{suffix}\n"


def scan_world(world, monkeypatch, rows, heads, *, git=None):
    """Install a decisions log of *rows* plus the waiver row and a git double for *heads*."""
    waiver = "| D-6000 | demo | Waiver: merged-without-witnesses; Waives-seats: demo:H | OPERATOR | 2026-11-14 |\n"
    world.decision_path.write_text(HEADER + "".join(rows) + waiver)
    git = git or ScanGit(heads)
    commits = [{"sha": h, "date": "2026-01-01", "merge": {"pr": n + 1}} for n, h in enumerate(heads)]
    monkeypatch.setattr("plangraph.keeper.context_sources.plan_item_commits", lambda *a, **kw: commits)
    monkeypatch.setattr("plangraph.keeper.context_sources.open_lanes", lambda *a: [])
    world.store.ctx["git"] = git
    return git


def count_reads(monkeypatch, limit=None):
    reads = []
    original = Path.read_text

    def counted(self, *args, **kwargs):
        if self.name == "DECISIONS.md":
            reads.append(1)
            assert limit is None or len(reads) <= limit, "decisions document re-read"
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counted)
    return reads


def test_three_heads_in_a_7000_row_log_read_the_document_twice(unseated_w, monkeypatch):
    world = unseated_w
    heads = [HEAD, "f" * 40, "e" * 40]
    rows = [f"| D-{n} | demo | filler row {n} | ROUTER | 2026-10-01 |\n" for n in range(10000, 17000)]
    rows += [go_row("D-5504", heads[0]), go_row("D-9001", heads[1]), go_row("D-9002", heads[2])]
    git = scan_world(world, monkeypatch, rows, heads)
    reads = count_reads(monkeypatch, limit=2)
    started = time.monotonic()
    result = world.store.close_merged(world.p("router"), "demo:H", "D-5504", waive_seats="D-6000")
    assert result["state"] == "done"
    assert time.monotonic() - started < 20
    assert git.fetches <= 2 and len(reads) <= 2
    ref = world.store._evidence("demo:H")[-1]["ref"]
    assert [p["head"] for p in ref["prs"]] == heads


def test_unchanged_refusals_remain(unseated_w, monkeypatch):
    world = unseated_w
    heads = [HEAD, "f" * 40]
    scan_world(world, monkeypatch, [go_row("D-5504", HEAD)], heads)
    with pytest.raises(Refused, match="merged_pr_go_missing"):
        world.store.close_merged(world.p("router"), "demo:H", "D-5504", waive_seats="D-6000")
    assert world.store._item("demo:H")["state"] == "in_review"



def long_history(path, merges=700, item_at=50):
    """A repository whose main has *merges* first-parent merges; merge *item_at* carries a Plan-Item commit."""
    import subprocess
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    lines, when = [], 1_760_000_000
    def commit(ref, mark, message, parents):
        nonlocal when
        when += 60
        lines.extend([f"commit {ref}", f"mark :{mark}",
                      f"committer T <t@example.invalid> {when} +0000",
                      f"data {len(message.encode())}", message])
        for n, parent in enumerate(parents):
            lines.append(f"{'from' if n == 0 else 'merge'} :{parent}")
        lines.append("")
    commit("refs/heads/main", 1, "base", [])
    mark = 2
    for n in range(1, merges + 1):
        body = f"work {n}" + ("\n\nPlan-Item: demo:H" if n == item_at else "")
        previous = 1 if n == 1 else mark - 1  # mark of the main tip before this PR
        commit(f"refs/heads/pr{n}", mark, body, [previous])
        commit("refs/heads/main", mark + 1, f"merge (#{n})", [previous, mark])
        mark += 2
    subprocess.run(["git", "-C", str(path), "fast-import", "--quiet"], input="\n".join(lines) + "\n",
                   text=True, check=True)


def test_item_650_merges_behind_main_closes(unseated_w, monkeypatch, tmp_path):
    import subprocess
    from plangraph.keeper.evidence_git import GitAdapter
    world = unseated_w
    repo = tmp_path / "long"
    long_history(repo)
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "pr50"], text=True,
                          capture_output=True, check=True).stdout.strip()
    world.decision_path.write_text(
        HEADER + go_row("D-5504", head)
        + "| D-6000 | demo | Waiver: merged-without-witnesses; Waives-seats: demo:H | OPERATOR | 2026-11-14 |\n")
    monkeypatch.setattr("plangraph.keeper.context_sources.plan_item_commits",
                        world.real_plan_item_commits)
    monkeypatch.setattr("plangraph.keeper.context_sources.open_lanes", lambda *a: [])
    world.store.ctx["git"] = GitAdapter({"demo": {"path": str(repo), "ref": "main"}})
    result = world.store.close_merged(world.p("router"), "demo:H", "D-5504", waive_seats="D-6000")
    assert result["state"] == "done"
    assert [p["head"] for p in world.store._evidence("demo:H")[-1]["ref"]["prs"]] == [head]


def undated_row(did, head):
    return f"| {did} | demo | GO #{did[2:]} demo:H; GO-HEAD {head} | ROUTER |\n"


def two_head_close(world, monkeypatch, second_rows):
    heads = [HEAD, "f" * 40]
    scan_world(world, monkeypatch, [go_row("D-5504", HEAD), *second_rows], heads)
    return world.store.close_merged(world.p("router"), "demo:H", "D-5504", waive_seats="D-6000")


def test_dated_go_row_wins_over_an_older_undated_one(unseated_w, monkeypatch):
    result = two_head_close(unseated_w, monkeypatch, [
        undated_row("D-4000", "f" * 40), go_row("D-9001", "f" * 40)])
    assert result["state"] == "done"
    prs = unseated_w.store._evidence("demo:H")[-1]["ref"]["prs"]
    assert {p["head"]: p["decision_id"] for p in prs} == {HEAD: "D-5504", "f" * 40: "D-9001"}


@pytest.mark.parametrize("rows,date", [
    ([undated_row("D-4000", "f" * 40)], None),
    ([undated_row("D-4000", "f" * 40), go_row("D-9001", "f" * 40, date="2026-11-13")], "2026-11-13")])
def test_undated_or_late_go_rows_still_refuse(unseated_w, monkeypatch, rows, date):
    with pytest.raises(Refused, match="operator_seat_waiver_go_date_ineligible"):
        two_head_close(unseated_w, monkeypatch, rows)
    assert unseated_w.store._item("demo:H")["state"] == "in_review"


LOG_HEADER = "| id | project | decision | by | why / evidence |\n|---|---|---|---|---|\n"


def test_single_head_go_dated_in_the_decision_cell_passes_the_d6295_cutoff(unseated_w, monkeypatch):
    """Grant and waiver rows in the decisions-log grammar (Date: and Cutoff: inside the DECISION cell)."""
    world = unseated_w
    grant = (f"| D-6283 | demo | GO {HEAD} demo:H - second retroactive grant row for the keeper close-merged "
             "check, covering PR #415; the head is the second parent of the merge commit the keeper binds. "
             "Date: 2026-11-15T19:54:23Z | operator, router | git rev-list on origin main; D-5865 |\n")
    waiver = ("| D-6295 | demo | Waiver: merged-without-witnesses (second row in the form of D-6142). "
              "Date: 2026-11-15T19:55:28Z; Basis: the same operator answers; "
              "Waives-seats: demo:H demo:OTHER; Cutoff: 2026-11-15T19:54:53Z (after every one of those merges). "
              "Close mechanics: rk.sh close-merged KEY D-id --waive-seats D-6295. | operator | D-5865, D-5909 |\n")
    world.decision_path.write_text(LOG_HEADER + grant + waiver)
    git = ScanGit([HEAD])
    monkeypatch.setattr("plangraph.keeper.context_sources.plan_item_commits",
                        lambda *a, **kw: [{"sha": HEAD, "date": "2026-11-05", "merge": {"pr": 415}}])
    monkeypatch.setattr("plangraph.keeper.context_sources.open_lanes", lambda *a: [])
    world.store.ctx["git"] = git
    result = world.store.close_merged(world.p("router"), "demo:H", "D-6283", waive_seats="D-6295")
    assert result["state"] == "done"
    assert world.store._evidence("demo:H")[-1]["ref"]["prs"] == [
        {"pr": 415, "head": HEAD, "decision_id": "D-6283"}]


def pr_wording_close(world, monkeypatch, text):
    row = f"| D-5809 | demo | (16:10Z, date -u) {text} | ROUTER | 2026-11-12 |\n"
    scan_world(world, monkeypatch, [row], [HEAD])
    return world.store.close_merged(world.p("router"), "demo:H", "D-5809", waive_seats="D-6000")


def test_go_project_pr_wording_closes(unseated_w, monkeypatch):
    result = pr_wording_close(unseated_w, monkeypatch,
                              f"GO demo PR #840 H (high) at GO-HEAD {HEAD}: 4 files, CI green")
    assert result["state"] == "done"


@pytest.mark.parametrize("text", [
    f"GO demo PR #840 H at GO-HEAD {HEAD} and GO-HEAD {'f' * 40}",
    f"GO demo PR #840 H at GO-HEAD {'f' * 40}",
    f"GO other PR #840 H at GO-HEAD {HEAD}",
])
def test_go_project_pr_wording_refuses_other_heads(unseated_w, monkeypatch, text):
    with pytest.raises(Refused, match="go_row_head_mismatch"):
        pr_wording_close(unseated_w, monkeypatch, text)
    assert unseated_w.store._item("demo:H")["state"] == "in_review"


class FakeSession:
    json_mode = False

    def __init__(self, state, post_error=True):
        self.state, self.calls = state, []

    def call(self, method, verb, params=None, body=None, timeout=None):
        from plangraph.keeper.cli_verbs import Unreachable
        self.calls.append((method, verb, timeout))
        if method == "POST":
            raise Unreachable("timed out")
        return {"key": "demo:H", "state": self.state}


def close_args():
    return SimpleNamespace(key="demo:H", decision_id="D-5504", families=None, from_open=False,
                           apply=False, waive_seats="D-6000")


def test_client_timeout_after_a_committed_close_reads_back_and_exits_zero(capsys):
    from plangraph.keeper.cli_output_verbs import cmd_close_merged
    from plangraph.keeper.cli_verbs import SLOW_TIMEOUT
    session = FakeSession("done")
    assert cmd_close_merged(close_args(), session) == 0
    captured = capsys.readouterr()
    assert "done" in captured.out and "closed by this request or a concurrent one" in captured.err
    assert session.calls[0] == ("POST", "close_merged", SLOW_TIMEOUT) and SLOW_TIMEOUT >= 120


def test_from_open_apply_timeout_after_a_committed_close_reads_back_and_exits_zero(capsys):
    from plangraph.keeper.cli_output_verbs import cmd_close_merged
    args = close_args()
    args.from_open = args.apply = True
    session = FakeSession("done")
    assert cmd_close_merged(args, session) == 0
    assert "demo:H\tCLOSED\t" in capsys.readouterr().out
    assert [c[0] for c in session.calls] == ["POST", "GET"]


def test_client_timeout_without_a_commit_exits_non_zero(capsys):
    from plangraph.keeper.cli_output_verbs import cmd_close_merged
    assert cmd_close_merged(close_args(), FakeSession("in_review")) == 3
    assert "keeper unreachable" in capsys.readouterr().err


def test_an_earlier_dated_row_never_replaces_a_later_undated_supplied_id(unseated_w, monkeypatch):
    world = unseated_w
    scan_world(world, monkeypatch, [undated_row("D-1000", HEAD), go_row("D-999", HEAD)], [HEAD])
    with pytest.raises(Refused, match="operator_seat_waiver_go_date_ineligible"):
        world.store.close_merged(world.p("router"), "demo:H", "D-1000", waive_seats="D-6000")
    assert world.store._item("demo:H")["state"] == "in_review"


def test_a_go_row_edited_between_the_phases_refuses(unseated_w, monkeypatch):
    world = unseated_w
    heads = [HEAD, "f" * 40]
    rows = [go_row("D-5504", HEAD), go_row("D-9001", "f" * 40)]
    scan_world(world, monkeypatch, rows, heads)
    original = world.store._write

    def edited_write():
        world.decision_path.write_text(world.decision_path.read_text().replace(
            "Waives-seats: demo:H", "Waives-seats: demo:H demo:Z"))
        return original()

    monkeypatch.setattr(world.store, "_write", edited_write)
    with pytest.raises(Refused, match="merged_seat_waiver_changed"):
        world.store.close_merged(world.p("router"), "demo:H", "D-5504", waive_seats="D-6000")
    assert world.store._item("demo:H")["state"] == "in_review"


def test_a_later_dated_row_replaces_an_undated_supplied_id_across_digit_widths(unseated_w, monkeypatch):
    world = unseated_w
    # D-1000 sorts before D-999 as text, so it meets the preset holder D-999 first.
    scan_world(world, monkeypatch, [undated_row("D-999", HEAD), go_row("D-1000", HEAD)], [HEAD])
    result = world.store.close_merged(world.p("router"), "demo:H", "D-999", waive_seats="D-6000")
    assert result["state"] == "done"
    prs = world.store._evidence("demo:H")[-1]["ref"]["prs"]
    assert [p["decision_id"] for p in prs] == ["D-1000"]


def test_overlapping_snapshots_on_two_threads_do_not_share_text(tmp_path):
    import threading
    from plangraph.keeper.evidence_decisions import DecisionsAdapter
    path = tmp_path / "DECISIONS.md"
    path.write_text("| D-1 | demo | first | ROUTER | 2026-11-12 |\n")
    adapter = DecisionsAdapter(path)
    entered, proceed, seen = threading.Event(), threading.Event(), {}

    def first():
        with adapter.snapshot():
            seen["a1"] = adapter._document(_strict=True)
            entered.set()
            proceed.wait(5)

    worker = threading.Thread(target=first)
    worker.start()
    entered.wait(5)
    path.write_text("| D-1 | demo | second | ROUTER | 2026-11-12 |\n")
    with adapter.snapshot():
        seen["b"] = adapter._document(_strict=True)
    proceed.set()
    worker.join()
    assert "first" in seen["a1"] and "second" in seen["b"]
