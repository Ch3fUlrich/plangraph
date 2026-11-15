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

