import fcntl
import json
import subprocess

import pytest

from keeper_util import World, defn
from plangraph.keeper.errors import Refused
from plangraph.planner import phase_finish
from phase_finish_support import FakeGitHub, finish_cards, gate, git_repo

@pytest.fixture
def w(tmp_path):
    world = World(tmp_path / "keeper")
    yield world
    world.close()

class ExplodingStore:
    def __getattr__(self, name):
        raise AssertionError(f"store was read via {name}")

def test_all_cards_done_opens_pr_and_merges_when_ci_green(w, tmp_path):
    gate(w)
    finish_cards(w, "demo:C1", "demo:C2")
    root, head = git_repo(tmp_path)
    branch = phase_finish.phase_branch("demo:PH-X")
    github = FakeGitHub(ci=True)

    result = phase_finish.finish_phase(
        w.store,
        w.p("router"),
        "demo:PH-X",
        branch,
        head,
        repo_root=root,
        github=github,
    )

    assert result["state"] == "merged"
    assert result["pr"]["number"] == 7
    assert result["cards"] == ["demo:C1", "demo:C2"]
    assert result["merge_sha"] == github.merge_sha
    rows = w.store.conn.execute(
        "SELECT key, ref FROM evidence WHERE kind='commit+ci'"
    ).fetchall()
    assert len(rows) == 2
    assert {row[0] for row in rows} == {"demo:C1", "demo:C2"}
    assert all(json.loads(row[1]) == {"commit": github.merge_sha} for row in rows)
    assert [c[0] for c in github.calls] == [
        "find_pr",
        "push_branch",
        "open_pr",
        "ci_green",
        "merge_pr",
    ]

def test_waiting_ci_does_not_merge(w, tmp_path):
    gate(w)
    finish_cards(w, "demo:C1", "demo:C2")
    root, head = git_repo(tmp_path)
    github = FakeGitHub(ci=False)

    result = phase_finish.finish_phase(
        w.store,
        w.p("router"),
        "demo:PH-X",
        phase_finish.phase_branch("demo:PH-X"),
        head,
        repo_root=root,
        github=github,
    )

    assert result["state"] == "waiting_ci"
    assert "merge_pr" not in [c[0] for c in github.calls]
    assert w.store.conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 0

def test_already_merged_pr_is_idempotent(w, tmp_path):
    gate(w)
    finish_cards(w, "demo:C1", "demo:C2")
    root, head = git_repo(tmp_path)
    branch = phase_finish.phase_branch("demo:PH-X")
    github = FakeGitHub(
        pr={
            "number": 3,
            "url": "https://example.invalid/pr/3",
            "state": "merged",
            "head": head,
        }
    )

    result = phase_finish.finish_phase(
        w.store,
        w.p("router"),
        "demo:PH-X",
        branch,
        head,
        repo_root=root,
        github=github,
    )

    assert result["state"] == "merged"
    assert result["pr"]["number"] == 3
    assert github.calls == [("find_pr", branch)]
    assert w.store.conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 0

def test_open_pr_is_reused(w, tmp_path):
    gate(w)
    finish_cards(w, "demo:C1", "demo:C2")
    root, head = git_repo(tmp_path)
    github = FakeGitHub(
        pr={
            "number": 4,
            "url": "https://example.invalid/pr/4",
            "state": "open",
            "head": head,
        },
        ci=True,
    )

    result = phase_finish.finish_phase(
        w.store,
        w.p("router"),
        "demo:PH-X",
        phase_finish.phase_branch("demo:PH-X"),
        head,
        repo_root=root,
        github=github,
    )

    assert result["state"] == "merged"
    assert result["pr"]["number"] == 4
    assert "open_pr" not in [c[0] for c in github.calls]
    assert ("merge_pr", 4, head) in github.calls

def test_card_not_done_refused(w):
    gate(w)
    finish_cards(w, "demo:C1")
    github = FakeGitHub()

    with pytest.raises(Refused) as ei:
        phase_finish.finish_phase(
            w.store,
            w.p("router"),
            "demo:PH-X",
            phase_finish.phase_branch("demo:PH-X"),
            "a" * 40,
            repo_root=w.root if hasattr(w, "root") else ".",
            github=github,
        )

    assert ei.value.code == "bad_state"
    assert "demo:C2" in str(ei.value)
    assert github.calls == []

def test_no_cards_refused(w):
    w.store.create_items(w.p("router"), [
        defn("PH-E", kind="gate"),
        defn("GOAL-E", ["PH-E"], kind="goal"),
    ])
    github = FakeGitHub()

    with pytest.raises(Refused) as ei:
        phase_finish.finish_phase(
            w.store,
            w.p("router"),
            "demo:PH-E",
            phase_finish.phase_branch("demo:PH-E"),
            "a" * 40,
            repo_root=".",
            github=github,
        )

    assert ei.value.code == "bad_state"

def test_non_router_refused_first(w):
    for principal in (w.p("workerA"), w.p("l1main")):
        with pytest.raises(Refused) as ei:
            phase_finish.finish_phase(
                ExplodingStore(),
                principal,
                "demo:PH-X",
                phase_finish.phase_branch("demo:PH-X"),
                "a" * 40,
                repo_root=".",
                github=FakeGitHub(),
            )
        assert ei.value.code == "forbidden"

def test_branch_must_match_key(w):
    with pytest.raises(Refused) as ei:
        phase_finish.finish_phase(
            w.store,
            w.p("router"),
            "demo:PH-X",
            "phase/not-it",
            "a" * 40,
            repo_root=".",
            github=FakeGitHub(),
        )
    assert ei.value.code == "validation"

def test_bad_head_refused(w):
    with pytest.raises(Refused) as ei:
        phase_finish.finish_phase(
            w.store,
            w.p("router"),
            "demo:PH-X",
            phase_finish.phase_branch("demo:PH-X"),
            "A" * 40,
            repo_root=".",
            github=FakeGitHub(),
        )
    assert ei.value.code == "validation"

def test_unknown_and_non_gate_refused(w):
    gate(w)

    with pytest.raises(Refused) as ei:
        phase_finish.finish_phase(
            w.store,
            w.p("router"),
            "demo:C1",
            phase_finish.phase_branch("demo:C1"),
            "a" * 40,
            repo_root=".",
            github=FakeGitHub(),
        )
    assert ei.value.code == "validation"

    with pytest.raises(Refused) as ei:
        phase_finish.finish_phase(
            w.store,
            w.p("router"),
            "demo:NOPE",
            phase_finish.phase_branch("demo:NOPE"),
            "a" * 40,
            repo_root=".",
            github=FakeGitHub(),
        )
    assert ei.value.code == "not_found"

def test_local_tip_mismatch_is_conflict(w, tmp_path):
    gate(w)
    finish_cards(w, "demo:C1", "demo:C2")
    root, head = git_repo(tmp_path)
    wrong = ("0" if head[0] != "0" else "1") + head[1:]
    github = FakeGitHub()

    with pytest.raises(Refused) as ei:
        phase_finish.finish_phase(
            w.store,
            w.p("router"),
            "demo:PH-X",
            phase_finish.phase_branch("demo:PH-X"),
            wrong,
            repo_root=root,
            github=github,
        )

    assert ei.value.code == "conflict"
    assert github.calls == []

def test_github_error_maps_to_unavailable(w, tmp_path):
    gate(w)
    finish_cards(w, "demo:C1", "demo:C2")
    root, head = git_repo(tmp_path)
    github = FakeGitHub(fail=phase_finish.GitHubError("boom"))

    with pytest.raises(Refused) as ei:
        phase_finish.finish_phase(
            w.store,
            w.p("router"),
            "demo:PH-X",
            phase_finish.phase_branch("demo:PH-X"),
            head,
            repo_root=root,
            github=github,
        )

    assert ei.value.code == "unavailable"

def test_busy_lock_is_conflict(w, tmp_path):
    gate(w)
    finish_cards(w, "demo:C1", "demo:C2")
    root, head = git_repo(tmp_path)
    lock_path = tmp_path / "phase-finish.lock"
    github = FakeGitHub(ci=True)

    fh = open(lock_path, "a")
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(Refused) as ei:
            phase_finish.finish_phase(
                w.store,
                w.p("router"),
                "demo:PH-X",
                phase_finish.phase_branch("demo:PH-X"),
                head,
                repo_root=root,
                github=github,
                lock_path=lock_path,
            )
        assert ei.value.code == "conflict"
    finally:
        fh.close()

    result = phase_finish.finish_phase(
        w.store,
        w.p("router"),
        "demo:PH-X",
        phase_finish.phase_branch("demo:PH-X"),
        head,
        repo_root=root,
        github=github,
        lock_path=lock_path,
    )
    assert result["state"] == "merged"

def test_phase_branch_rules():
    assert phase_finish.phase_branch("demo:PH-X") == "phase/h/4095fd20aa975021"
    for key in ("", ".x", "x.", "a.lock", "a..b", "x" * 121):
        with pytest.raises(Refused) as ei:
            phase_finish.phase_branch(key)
        assert ei.value.code == "validation"

def test_finish_locked_rejects_merged_pr_with_different_head(tmp_path):
    branch = phase_finish.phase_branch("demo:PH-X")
    head = "a" * 40
    other = "b" * 40
    gh = FakeGitHub(pr={"number": 7, "state": "merged", "head": other})
    with pytest.raises(Refused) as ei:
        phase_finish._finish_locked(
            root=tmp_path, base="main", key="demo:PH-X", branch=branch,
            head=head, cards=[], github=gh,
        )
    assert ei.value.code == "conflict"
    assert gh.calls == [("find_pr", branch)]

def test_finish_locked_opens_pr_for_closed_unmerged(tmp_path):
    branch = phase_finish.phase_branch("demo:PH-X")
    head = "a" * 40
    gh = FakeGitHub(pr={"number": 7, "state": "closed", "head": "b" * 40})
    gh.push_branch = lambda *args, **kwargs: None
    def open_pr(*args, **kwargs):
        gh.calls.append(("open_pr",) + args)
        return {"number": 7, "state": "open", "head": head, "url": "u"}
    gh.open_pr = open_pr
    gh.ci_green = lambda _head: False
    phase_finish._finish_locked(
        root=tmp_path, base="main", key="demo:PH-X", branch=branch,
        head=head, cards=[], github=gh,
    )
    assert any(call[0] == "open_pr" for call in gh.calls)

def test_no_store_write(w, tmp_path):
    gate(w)
    finish_cards(w, "demo:C1", "demo:C2")
    root, head = git_repo(tmp_path)
    before_count = w.store.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    before_events = w.store.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    before_evidence = w.store.conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]
    before_state = w.store.conn.execute(
        "SELECT state FROM items WHERE key=?", ("demo:PH-X",)
    ).fetchone()[0]

    result = phase_finish.finish_phase(
        w.store,
        w.p("router"),
        "demo:PH-X",
        phase_finish.phase_branch("demo:PH-X"),
        head,
        repo_root=root,
        github=FakeGitHub(ci=False),
    )

    after_count = w.store.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    after_state = w.store.conn.execute(
        "SELECT state FROM items WHERE key=?", ("demo:PH-X",)
    ).fetchone()[0]
    assert result["state"] == "waiting_ci"
    assert after_count == before_count
    assert after_state == before_state
    assert w.store.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before_events
    assert w.store.conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == before_evidence
    assert w.store.conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 0
