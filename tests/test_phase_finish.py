import fcntl
import subprocess

import pytest

from keeper_util import World, defn
from plangraph.keeper.errors import Refused
from plangraph.planner import phase_finish

@pytest.fixture
def w(tmp_path):
    world = World(tmp_path / "keeper")
    yield world
    world.close()

def gate(w):
    w.store.create_items(
        w.p("router"),
        [
            defn("C1"),
            defn("C2"),
            defn("PH-X", ["C1", "C2"], kind="gate"),
            defn("GOAL", ["PH-X"], kind="goal"),
        ],
    )

def finish_cards(w, *keys):
    for key in keys:
        w.store.conn.execute("UPDATE items SET state='done' WHERE key=?", (key,))
    w.store.conn.commit()

def git_repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "x",
        ],
        cwd=root,
        check=True,
    )
    subprocess.run(["git", "branch", "-M", "phase/demo-PH-X"], cwd=root, check=True)
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True
    ).strip()
    return root, head

class FakeGitHub:
    def __init__(self, pr=None, ci=True, fail=None):
        self.pr = pr
        self.ci = ci
        self.fail = fail
        self.calls = []

    def _pr(self, number, state, head):
        return {
            "number": number,
            "url": f"https://example.invalid/pr/{number}",
            "state": state,
            "head": head,
        }

    def push_branch(self, repo_root, branch, head):
        self.calls.append(("push_branch", repo_root, branch, head))
        if self.fail is not None:
            raise self.fail

    def find_pr(self, branch):
        self.calls.append(("find_pr", branch))
        return self.pr

    def open_pr(self, branch, base, title, body):
        self.calls.append(("open_pr", branch, base, title, body))
        return self._pr(7, "open", "")

    def ci_green(self, head):
        self.calls.append(("ci_green", head))
        return self.ci

    def merge_pr(self, number, head):
        self.calls.append(("merge_pr", number, head))
        return self._pr(number, "merged", head)

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
                "phase/demo-PH-X",
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
    assert phase_finish.phase_branch("demo:PH-X") == "phase/demo-PH-X"
    for key in ("", ".x", "x.", "a.lock", "a..b", "x" * 121):
        with pytest.raises(Refused) as ei:
            phase_finish.phase_branch(key)
        assert ei.value.code == "validation"

def test_no_store_write(w, tmp_path):
    gate(w)
    finish_cards(w, "demo:C1", "demo:C2")
    root, head = git_repo(tmp_path)
    before_count = w.store.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
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
        github=FakeGitHub(ci=True),
    )

    after_count = w.store.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    after_state = w.store.conn.execute(
        "SELECT state FROM items WHERE key=?", ("demo:PH-X",)
    ).fetchone()[0]
    assert result["state"] == "merged"
    assert after_count == before_count
    assert after_state == before_state
