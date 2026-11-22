"""Close-merged scan cost: one fetch and one document read per request phase."""
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from plangraph.keeper.errors import Refused
from test_keeper_close_merged import HEAD, MERGE, unseated_w, w  # noqa: F401

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
                           apply=True, waive_seats="D-6000")


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


def test_client_timeout_on_dry_run_does_not_read_back_or_report_a_close(capsys):
    from plangraph.keeper.cli_output_verbs import cmd_close_merged
    args = close_args()
    args.apply = False
    session = FakeSession("done")
    assert cmd_close_merged(args, session) == 3
    assert [call[0] for call in session.calls] == ["POST"]
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


# ---- LE-CLOSE-MERGED-REFUSES-VALID-GO-ROWS and KEEPER-CLOSE-MERGED-NO-GO-WAIVER ----

def _git(repo, *args, date="2026-11-01T12:00:00Z"):
    import os
    import subprocess
    env = {**os.environ, "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.invalid",
           "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
    return subprocess.run(["git", "-C", str(repo), *args], env=env, check=True,
                          capture_output=True, text=True).stdout.strip()


def merged_repo(path, parts):
    """A repository whose main has one merged PR per ``(pr, key, date)``; ``pr`` None = direct commit.

    Returns the PR head per number (``None`` keyed by the direct commit's sha list).
    """
    import subprocess
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    _git(path, "commit", "-q", "--allow-empty", "-m", "base")
    heads = {}
    for pr, key, date in parts:
        message = "work" + (f"\n\nPlan-Item: {key}" if key else "")
        if pr is None:
            _git(path, "commit", "-q", "--allow-empty", "-m", message, date=date)
            heads.setdefault(None, []).append(_git(path, "rev-parse", "HEAD"))
            continue
        _git(path, "checkout", "-q", "-b", f"pr{pr}")
        _git(path, "commit", "-q", "--allow-empty", "-m", message, date=date)
        heads[pr] = _git(path, "rev-parse", "HEAD")
        _git(path, "checkout", "-q", "main")
        _git(path, "merge", "-q", "--no-ff", "-m", f"Merge pull request #{pr} from x/pr{pr}",
             f"pr{pr}", date=date)
    return heads


def real_git_world(world, monkeypatch, tmp_path, parts, projects=("demo",)):
    from plangraph.keeper.evidence_git import GitAdapter
    repo = tmp_path / "repo"
    heads = merged_repo(repo, parts)
    world.store.ctx["git"] = GitAdapter({p: {"path": str(repo), "ref": "main"} for p in projects})
    monkeypatch.setattr("plangraph.keeper.context_sources.plan_item_commits",
                        world.real_plan_item_commits)
    return heads


GO_FORMS = [
    # the shapes of D-5826 and D-5802, D-5152 and D-5761 (project and item renamed to the fixture's)
    "(17:22Z, date -u) GO demo PR #841 demo:A at GO-HEAD {head} (lane, merges): 8 files +329/-41, CI success",
    "(15:46Z, date -u) GO demo PR #841 demo:A (D-5782 (1)) at GO-HEAD {head}: 2 files +3/-1, MERGEABLE CLEAN",
    "(18:5xZ) GO #841 A slice 2 with --merge, merge only by replan, after #737; GO-HEAD {head}. Seat ACCEPT",
    "(13:22Z, date -u) RULINGS. (1) GO demo PR 841 demo:A slice A, mark ready; GO-HEAD {head}; a moved head "
    "voids this. (2) DEFINED X under Y. (3) #829 closed unmerged.",
]


@pytest.mark.parametrize("form", GO_FORMS)
def test_go_row_binds_to_the_merged_pr_it_names(w, monkeypatch, tmp_path, form):
    """The key's Plan-Item commit sits in another merge; the row names the PR that merged its own head."""
    heads = real_git_world(w, monkeypatch, tmp_path, [(841, None, "2026-11-01T12:00:00Z"),
                                                       (900, "demo:A", "2026-11-02T12:00:00Z")])
    w.decision_path.write_text(HEADER + f"| D-5504 | demo | {form.format(head=heads[841])} | ROUTER | 2026-11-05 |\n")
    assert w.store.close_merged(w.p("router"), "demo:A", "D-5504", ["google"])["state"] == "done"
    assert w.store._evidence("demo:A")[-1]["ref"]["merged_head"] == heads[841]


@pytest.mark.parametrize("case", ["other_head", "pr_number", "not_named", "unmerged_head"])
def test_go_row_binding_refusals_name_both_heads(w, monkeypatch, tmp_path, case):
    heads = real_git_world(w, monkeypatch, tmp_path, [(841, None, "2026-11-01T12:00:00Z"),
                                                       (900, "demo:A", "2026-11-02T12:00:00Z")])
    _git(tmp_path / "repo", "checkout", "-q", "-b", "lane/loose")
    _git(tmp_path / "repo", "commit", "-q", "--allow-empty", "-m", "loose")
    loose = _git(tmp_path / "repo", "rev-parse", "HEAD")
    _git(tmp_path / "repo", "checkout", "-q", "main")
    row = {"other_head": f"GO demo PR #841 demo:A at GO-HEAD {'f' * 40}",
           "pr_number": f"GO demo PR #999 demo:A at GO-HEAD {heads[841]}",
           "not_named": f"GO demo PR #841 demo:OTHER at GO-HEAD {heads[841]}",
           "unmerged_head": f"GO demo PR #841 demo:A at GO-HEAD {loose}"}[case]
    w.decision_path.write_text(HEADER + f"| D-5504 | demo | {row} | ROUTER | 2026-11-05 |\n")
    with pytest.raises(Refused) as exc:
        w.store.close_merged(w.p("router"), "demo:A", "D-5504", ["google"])
    assert "go_row_head_mismatch" in exc.value.msg
    named = {"other_head": "f" * 12, "unmerged_head": loose[:12]}.get(case, heads[841][:12])
    assert named in exc.value.msg and heads[900][:12] in exc.value.msg
    assert w.store._item("demo:A")["state"] == "in_review"


def test_not_go_refusal_names_the_merged_heads(w, monkeypatch, tmp_path):
    heads = real_git_world(w, monkeypatch, tmp_path, [(900, "demo:A", "2026-11-02T12:00:00Z")])
    w.decision_path.write_text(HEADER + f"| D-5504 | demo | RULINGS. (1) HOLD {heads[900]} | ROUTER | 2026-11-05 |\n")
    with pytest.raises(Refused) as exc:
        w.store.close_merged(w.p("router"), "demo:A", "D-5504", ["google"])
    assert "go_row_not_go" in exc.value.msg and heads[900][:12] in exc.value.msg


def test_item_naming_its_merge_repository_is_verified_there(w, monkeypatch, tmp_path):
    from plangraph.keeper.evidence_git import GitAdapter
    repo = tmp_path / "other-repo"
    heads = merged_repo(repo, [(165, None, "2026-11-01T12:00:00Z")])
    empty = tmp_path / "demo-repo"
    merged_repo(empty, [])
    w.store.ctx["git"] = GitAdapter({"demo": {"path": str(empty), "ref": "main"},
                                     "other": {"path": str(repo), "ref": "main"}})
    monkeypatch.setattr("plangraph.keeper.context_sources.plan_item_commits", w.real_plan_item_commits)
    w.decision_path.write_text(HEADER + f"| D-5504 | demo | GO #165 demo:A; GO-HEAD {heads[165]} | ROUTER | 2026-11-05 |\n")
    with pytest.raises(Refused, match="merged_on_default_missing"):
        w.store.close_merged(w.p("router"), "demo:A", "D-5504", ["google"])
    router = w.p("router")
    before = w.store._item("demo:A")
    _git(repo, "checkout", "-qb", "unmerged")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "unmerged")
    loose = _git(repo, "rev-parse", "HEAD")
    for project, commit, reason in [
            ("nowhere", heads[165], "merge_repo_unknown"),
            ("other", loose, "merge_repo_commit_not_on_default")]:
        w.store.edit_definition(router, "demo:A", {"note": f"Merge-Repo: {project} {commit}"})
        assert w.store._item("demo:A")["state"] == "in_review"
        with pytest.raises(Refused, match=reason):
            w.store.close_merged(router, "demo:A", "D-5504", ["google"])
        audit = w.conn.execute("SELECT msg FROM audit_log WHERE action='close_merged' ORDER BY id DESC LIMIT 1").fetchone()
        assert reason in audit["msg"]
    landed = _git(repo, "rev-parse", "main")
    _git(repo, "checkout", "-qb", "later", "main")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "later work")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "-m", "later merge (#166)", "later")
    w.store.edit_definition(router, "demo:A", {"note": f"Merge-Repo: other {landed}"})
    edited = w.store._item("demo:A")
    assert [edited[f] for f in ("state", "epoch", "round")] == [before[f] for f in ("state", "epoch", "round")]
    assert w.store.close_merged(w.p("router"), "demo:A", "D-5504", ["google"])["state"] == "done"
    audit = w.conn.execute("SELECT msg FROM audit_log WHERE action='close_merged' ORDER BY id DESC LIMIT 1").fetchone()
    assert "merged_on_default" in audit["msg"]
    w.conn.execute("UPDATE items SET description=? WHERE key='demo:H'", ("Merge-Repo: nowhere",))
    with pytest.raises(Refused, match="merge_repo_unknown"):
        w.store.close_merged(w.p("router"), "demo:H", "D-5504", ["google", "deepseek"])


@pytest.mark.parametrize("evidence", ["trailer", "merge-subject", "wrong-pr", "wrong-project", "duplicate-head"])
@pytest.mark.parametrize("state", ["open", "in_review"])
def test_close_merged_reads_the_data_default_only_with_a_note(w, monkeypatch, tmp_path, evidence, state):
    from plangraph.keeper import context_sources
    from plangraph.keeper.evidence_git import GitAdapter

    primary, data = tmp_path / "demo", tmp_path / "data"
    data_project = "fixture-secondary"
    merged_repo(primary, [])
    key = "demo:A"
    heads = merged_repo(data, [(165, key if evidence == "trailer" else None, "2026-11-01T12:00:00Z")])
    w.store.ctx["git"] = GitAdapter({"demo": str(primary), data_project: str(data)})
    monkeypatch.setattr(context_sources, "plan_item_commits", w.real_plan_item_commits)
    monkeypatch.setattr(context_sources, "open_lanes", lambda *a, **kw: [])
    pr = 166 if evidence == "wrong-pr" else 165
    project = "unrelated" if evidence == "wrong-project" else data_project
    head = f"GO-HEAD {heads[165]}"
    if evidence == "duplicate-head":
        head += f"; {head}"
    w.decision_path.write_text(HEADER + f"| D-5504 | {project} | GO #{pr} {data_project}, "
                               f"item demo:OTHER and A; {head} | ROUTER | 2026-11-05 |\n")
    router = w.p("router")
    w.conn.execute("UPDATE items SET state=? WHERE key=?", (state, key))

    def close():
        return w.store.close_merged(router, key, "D-5504", ["google"],
                                    from_open=state == "open", apply=True)

    with pytest.raises(Refused, match="merged_on_default_missing"):
        close()
    w.store.edit_definition(router, key, {"note": f"Merge-Repo: {data_project}"})
    if evidence in ("wrong-pr", "wrong-project", "duplicate-head"):
        with pytest.raises(Refused, match="merged_on_default_missing"):
            close()
        assert w.store._item(key)["state"] == state
    else:
        assert close()["state"] == "done"
        assert w.store._evidence(key)[-1]["ref"]["merged_head"] == heads[165]


@pytest.mark.parametrize("state", ["MERGED", "OPEN", "CLOSED"])
def test_merge_repo_note_verifies_a_hosted_pr(w, monkeypatch, tmp_path, state):
    import json
    import subprocess

    from plangraph.keeper.evidence_git import GitAdapter
    repo = tmp_path / "other"
    heads = merged_repo(repo, [(165, None, "2026-11-01T12:00:00Z")])
    landed = _git(repo, "rev-parse", "main")
    _git(repo, "remote", "add", "origin", "https://github.com/example/other")
    empty = tmp_path / "demo"
    merged_repo(empty, [])
    w.store.ctx["git"] = GitAdapter({"demo": {"path": str(empty), "ref": "main"},
                                     "other": {"path": str(repo), "ref": "main"}})
    monkeypatch.setattr("plangraph.keeper.context_sources.plan_item_commits", w.real_plan_item_commits)
    w.decision_path.write_text(
        HEADER + f"| D-5504 | demo | GO other PR #165 demo:A; GO-HEAD {heads[165]} | ROUTER | 2026-11-05 |\n")
    calls = []
    run = subprocess.run

    def hosted(args, **kwargs):
        if args[0] != "gh":
            return run(args, **kwargs)
        calls.append(args)
        info = {"state": state, "mergeCommit": {"oid": landed}, "headRefOid": heads[165]}
        return subprocess.CompletedProcess(args, 0, json.dumps(info))

    monkeypatch.setattr(subprocess, "run", hosted)
    router = w.p("router")
    w.store.edit_definition(router, "demo:A", {"note": "Merge-Repo: other #165"})
    if state == "MERGED":
        result = w.store.close_merged(router, "demo:A", "D-5504", ["google"])
        assert result["state"] == "done"
    else:
        with pytest.raises(Refused, match=r"merge_repo_pr_not_merged: other #165"):
            w.store.close_merged(router, "demo:A", "D-5504", ["google"])
        assert w.store._item("demo:A")["state"] == "in_review"
    assert calls and all(args[1:6] == ["pr", "view", "165", "--repo", "example/other"]
                         for args in calls)


WAIVER = ("| D-6400 | demo | Waiver: merged-without-witnesses (operator order); Date: 2026-11-20T14:55:00Z; "
          "Waives-seats: {keys} | {by} | 2026-11-20 |\n")


def waiver_world(w, monkeypatch, tmp_path, parts, keys="demo:A", by="OPERATOR", text=None):
    heads = real_git_world(w, monkeypatch, tmp_path, parts)
    w.decision_path.write_text(HEADER + (text or WAIVER.format(keys=keys, by=by)))
    return heads


def waiver_close(w, key="demo:A", **kwargs):
    return w.store.close_merged(w.p("router"), key, "D-6400", waive_seats="D-6400", **kwargs)


def test_operator_waiver_row_alone_closes_merged_and_direct_commits(w, monkeypatch, tmp_path):
    heads = waiver_world(w, monkeypatch, tmp_path, [(700, "demo:A", "2026-11-01T12:00:00Z"),
                                                    (None, "demo:A", "2026-11-03T12:00:00Z")])
    before = _evidence_count(w)
    dry = waiver_close(w)                       # no --apply: a dry run for an in_review item
    assert dry["would_close"] is True and dry["state"] == "in_review"
    assert _evidence_count(w) == before and w.store._item("demo:A")["state"] == "in_review"
    result = waiver_close(w, apply=True)
    assert result["state"] == "done"
    ref = w.store._evidence("demo:A")[-1]["ref"]
    assert ref["waiver"] == {"class": "merged-without-witnesses", "decision_id": "D-6400"}
    assert ref["seat_rule_waived_by"] == "D-6400" and ref["merged_head"] in {heads[700], heads[None][0]}
    assert {p["head"] for p in ref["prs"]} == {heads[700], heads[None][0]}
    assert {p["pr"] for p in ref["prs"]} == {700, None}
    assert w.store.verify_integrity() == []


def _evidence_count(w):
    return w.conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] + \
        w.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]


@pytest.mark.parametrize("case,reason", [
    ("unlisted", "operator_seat_waiver_scope_mismatch"),
    ("merged_at_cutoff", "operator_seat_waiver_merge_at_or_after_cutoff"),
    ("direct_after_cutoff", "operator_seat_waiver_merge_at_or_after_cutoff"),
    ("no_merge", "merged_on_default_missing"),
    ("not_operator", "operator_seat_waiver_invalid_or_ineligible"),
    ("no_prefix", "operator_seat_waiver_invalid_or_ineligible"),
])
def test_operator_waiver_row_refusals(w, monkeypatch, tmp_path, case, reason):
    parts = {"merged_at_cutoff": [(700, "demo:A", "2026-11-13T00:00:00Z")],
             "direct_after_cutoff": [(701, "demo:A", "2026-11-01T12:00:00Z"),
                                     (None, "demo:A", "2026-11-14T12:00:00Z")],
             "no_merge": [(702, "demo:OTHER", "2026-11-01T12:00:00Z")]}.get(
        case, [(700, "demo:A", "2026-11-01T12:00:00Z")])
    kwargs = {"unlisted": {"keys": "demo:H"}, "not_operator": {"by": "ROUTER"},
              "no_prefix": {"text": "| D-6400 | demo | merged-without-witnesses; Waives-seats: demo:A "
                                    "| OPERATOR | 2026-11-20 |\n"}}.get(case, {})
    waiver_world(w, monkeypatch, tmp_path, parts, **kwargs)
    with pytest.raises(Refused, match=reason):
        waiver_close(w, apply=True)
    assert w.store._item("demo:A")["state"] == "in_review"


def test_operator_waiver_refuses_an_item_with_an_open_lane(w, monkeypatch, tmp_path):
    waiver_world(w, monkeypatch, tmp_path, [(700, "demo:A", "2026-11-01T12:00:00Z")])
    _git(tmp_path / "repo", "branch", "lane/a-pending")
    _git(tmp_path / "repo", "checkout", "-q", "lane/a-pending")
    _git(tmp_path / "repo", "commit", "-q", "--allow-empty", "-m", "pending")
    _git(tmp_path / "repo", "checkout", "-q", "main")
    with pytest.raises(Refused, match="unmerged_work_or_lanes_unknown"):
        waiver_close(w, apply=True)
