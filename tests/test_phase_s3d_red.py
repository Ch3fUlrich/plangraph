import json
import subprocess

import pytest

from keeper_util import World, defn
from plangraph.keeper.errors import Refused
from plangraph.planner import phase_finish
from plangraph.planner.github_adapter import RestGitHub

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

def set_state(w, state, *keys):
    for key in keys:
        w.store.conn.execute("UPDATE items SET state=? WHERE key=?", (state, key))
    w.store.conn.commit()

def claim_as_runner(w, key, via="worker_claim"):
    w.store.conn.execute(
        "UPDATE items SET state='claimed', claimed_by='system', epoch=1 WHERE key=?",
        (key,),
    )
    w.store.conn.commit()
    w.store._append(
        w.p("system"),
        key,
        "claimed",
        {"epoch": 1, "claimed_by": "system", **({"via": via} if via else {})},
        1_700_000_000.0,
    )
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
        pr = self._pr(number, "merged", head)
        pr["merge_sha"] = "a" * 40
        return pr

def finish_in_review(w, tmp_path, *, ci=True):
    gate(w)
    set_state(w, "in_review", "demo:C1", "demo:C2")
    root, head = git_repo(tmp_path)
    github = FakeGitHub(ci=ci)
    result = phase_finish.finish_phase(
        w.store,
        w.p("router"),
        "demo:PH-X",
        phase_finish.phase_branch("demo:PH-X"),
        head,
        repo_root=root,
        github=github,
    )
    return result, github

def test_worker_complete_moves_claimed_card_to_in_review(w):
    gate(w)
    claim_as_runner(w, "demo:C1")
    w.store.worker_complete(
        w.p("system"), {"card": "demo:C1", "merged_sha": "abcdef1"}
    )
    row = w.store.conn.execute(
        "SELECT state FROM items WHERE key=?", ("demo:C1",)
    ).fetchone()
    assert row[0] == "in_review"

def test_worker_complete_on_non_phase_item_stays_claimed(w):
    w.store.create_items(
        w.p("router"),
        [
            defn("LONE"),
            defn("GOAL2", ["LONE"], kind="goal"),
        ],
    )
    set_state(w, "claimed", "demo:LONE")
    w.store.conn.execute(
        "UPDATE items SET claimed_by='system' WHERE key=?", ("demo:LONE",)
    )
    w.store.conn.commit()
    w.store.worker_complete(
        w.p("system"), {"card": "demo:LONE", "merged_sha": "abcdef1"}
    )
    row = w.store.conn.execute(
        "SELECT state FROM items WHERE key=?", ("demo:LONE",)
    ).fetchone()
    assert row[0] == "claimed"

def test_worker_complete_by_non_system_worker_stays_claimed(w):
    gate(w)
    set_state(w, "claimed", "demo:C1")
    w.store.conn.execute(
        "UPDATE items SET claimed_by='workerA' WHERE key=?", ("demo:C1",)
    )
    w.store.conn.commit()
    w.store.worker_complete(
        w.p("workerA"), {"card": "demo:C1", "merged_sha": "abcdef1"}
    )
    row = w.store.conn.execute(
        "SELECT state FROM items WHERE key=?", ("demo:C1",)
    ).fetchone()
    assert row[0] == "claimed"

def test_attach_merge_evidence_refused_before_pr_merged(w):
    gate(w)
    set_state(w, "in_review", "demo:C1", "demo:C2")
    github = FakeGitHub(
        pr={"number": 7, "url": "u", "state": "open", "head": "h"}
    )
    with pytest.raises(Refused) as exc:
        phase_finish.attach_merge_evidence(
            w.store, w.p("router"), "demo:PH-X", "a" * 40, github=github
        )
    assert exc.value.code == "bad_state"
    count = w.store.conn.execute(
        "SELECT COUNT(*) FROM evidence WHERE kind='commit+ci'"
    ).fetchone()[0]
    assert count == 0

def test_attach_merge_evidence_after_pr_merged(w):
    gate(w)
    set_state(w, "in_review", "demo:C1", "demo:C2")
    github = FakeGitHub(
        pr={"number": 7, "url": "u", "state": "merged", "head": "h"}
    )
    phase_finish.attach_merge_evidence(
        w.store, w.p("router"), "demo:PH-X", "a" * 40, github=github
    )
    rows = w.store.conn.execute(
        "SELECT key, ref FROM evidence WHERE kind='commit+ci'"
    ).fetchall()
    assert {row[0] for row in rows} == {"demo:C1", "demo:C2"}
    assert len(rows) == 2
    assert all("a" * 40 in row[1] for row in rows)
    with pytest.raises(Refused) as exc:
        phase_finish.attach_merge_evidence(
            w.store, w.p("router"), "demo:PH-X", "not-a-sha", github=github
        )
    assert exc.value.code == "validation"

def test_bypass_needs_worker_claim_marker(w):
    gate(w)
    claim_as_runner(w, "demo:C1", via=None)
    w.store.worker_complete(
        w.p("system"), {"card": "demo:C1", "merged_sha": "abcdef1"}
    )
    row = w.store.conn.execute(
        "SELECT state FROM items WHERE key=?", ("demo:C1",)
    ).fetchone()
    assert row[0] == "claimed"

def test_bypass_needs_kind_lane(w):
    w.store.create_items(
        w.p("router"),
        [
            defn("LONG", kind="decision"),
            defn("PH-D", ["LONG"], kind="gate"),
            defn("GOAL3", ["PH-D"], kind="goal"),
        ],
    )
    claim_as_runner(w, "demo:LONG")
    w.store.worker_complete(
        w.p("system"), {"card": "demo:LONG", "merged_sha": "abcdef1"}
    )
    row = w.store.conn.execute(
        "SELECT state FROM items WHERE key=?", ("demo:LONG",)
    ).fetchone()
    assert row[0] == "claimed"

def test_bypass_needs_gate_parent(w):
    w.store.create_items(
        w.p("router"),
        [
            defn("LONE2"),
            defn("GOAL4", ["LONE2"], kind="goal"),
        ],
    )
    claim_as_runner(w, "demo:LONE2")
    w.store.worker_complete(
        w.p("system"), {"card": "demo:LONE2", "merged_sha": "abcdef1"}
    )
    row = w.store.conn.execute(
        "SELECT state FROM items WHERE key=?", ("demo:LONE2",)
    ).fetchone()
    assert row[0] == "claimed"

def test_bypass_needs_running_phase(w):
    gate(w)
    claim_as_runner(w, "demo:C1")
    set_state(w, "cancelled", "demo:PH-X")
    w.store.worker_complete(
        w.p("system"), {"card": "demo:C1", "merged_sha": "abcdef1"}
    )
    row = w.store.conn.execute(
        "SELECT state FROM items WHERE key=?", ("demo:C1",)
    ).fetchone()
    assert row[0] == "claimed"

def test_finish_refuses_card_not_worker_complete(w, tmp_path):
    gate(w)
    set_state(w, "claimed", "demo:C1", "demo:C2")
    root, head = git_repo(tmp_path)
    github = FakeGitHub()
    with pytest.raises(Refused) as exc:
        phase_finish.finish_phase(
            w.store,
            w.p("router"),
            "demo:PH-X",
            phase_finish.phase_branch("demo:PH-X"),
            head,
            repo_root=root,
            github=github,
        )
    assert exc.value.code == "bad_state"
    assert "not worker-complete" in str(exc.value)
    assert github.calls == []

def test_finish_accepts_in_review_cards(w, tmp_path):
    result, _github = finish_in_review(w, tmp_path, ci=True)
    assert result["state"] == "merged"
    assert result["merge_sha"] == "a" * 40

def test_evidence_attached_to_each_card_after_green_merge(w, tmp_path):
    result, _github = finish_in_review(w, tmp_path, ci=True)
    assert result["state"] == "merged"
    rows = w.store.conn.execute(
        "SELECT key, kind, ref FROM evidence WHERE kind='commit+ci'"
    ).fetchall()
    assert {row[0] for row in rows} == {"demo:C1", "demo:C2"}
    assert len(rows) == 2
    assert all("a" * 40 in row[2] for row in rows)

def test_red_gate_attaches_no_evidence(w, tmp_path):
    result, github = finish_in_review(w, tmp_path, ci=False)
    assert result["state"] == "waiting_ci"
    assert "merge_pr" not in [c[0] for c in github.calls]
    count = w.store.conn.execute(
        "SELECT COUNT(*) FROM evidence WHERE kind='commit+ci'"
    ).fetchone()[0]
    assert count == 0

class FakeOpener:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, dict(headers), body))
        return self.replies.pop(0)

def token_file(tmp_path):
    path = tmp_path / "token.txt"
    path.write_text("tok-test", encoding="utf-8")
    path.chmod(0o600)
    return path

def make_github(tmp_path, replies):
    opener = FakeOpener(replies)
    gh = RestGitHub(
        "owner/repo",
        token_file(tmp_path),
        opener=opener,
        git_runner=None,
    )
    return gh, opener

def test_rest_github_merge_pr_returns_merge_sha(tmp_path):
    head = "d" * 40
    gh, opener = make_github(tmp_path, [(200, {"merged": True, "sha": "b" * 40})])
    result = gh.merge_pr(9, head)
    assert result["merge_sha"] == "b" * 40
    method, url, _headers, body = opener.calls[0]
    assert method == "PUT"
    assert url == "https://api.github.com/repos/owner/repo/pulls/9/merge"
    assert json.loads(body) == {"sha": head, "merge_method": "merge"}
