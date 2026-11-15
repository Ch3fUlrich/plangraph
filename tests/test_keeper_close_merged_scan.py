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
