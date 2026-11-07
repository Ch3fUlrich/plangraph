"""Shared phase-finish adapters and real-repository setup (not a test module)."""
import subprocess

from keeper_util import defn
from plangraph.planner import phase_finish


def git(root, *args, message=None):
    return subprocess.check_output(
        ["git", "-c", "user.name=keeper", "-c", "user.email=keeper@example.invalid",
         *args], cwd=root, input=message, text=True).strip()


def merge_commit(root, head):
    tree = git(root, "rev-parse", f"{head}^{{tree}}")
    base = git(root, "rev-parse", f"{head}^")
    return git(root, "commit-tree", tree, "-p", base, "-p", head,
               message="Merge phase demo:PH-X\n\nPlan-Item: demo:C1\nPlan-Item: demo:C2\n")


def gate(w):
    w.store.create_items(w.p("router"), [
        defn("C1"), defn("C2"), defn("PH-X", ["C1", "C2"], kind="gate"),
        defn("GOAL", ["PH-X"], kind="goal"),
    ])


def finish_cards(w, *keys):
    for key in keys:
        w.store.conn.execute("UPDATE items SET state='in_review' WHERE key=?", (key,))
    w.store.conn.commit()


def git_repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "commit", "--allow-empty", "-m", "base")
    git(root, "branch", "-M", phase_finish.phase_branch("demo:PH-X"))
    git(root, "branch", "-f", "main", "HEAD")
    git(root, "commit", "--allow-empty", "-m", "phase")
    return root, git(root, "rev-parse", "HEAD")


class FakeGitHub:
    def __init__(self, pr=None, ci=True, fail=None):
        self.pr, self.ci, self.fail = pr, ci, fail
        self.calls = []

    def _pr(self, number, state, head):
        return {"number": number, "url": f"https://example.invalid/pr/{number}",
                "state": state, "head": head}

    def push_branch(self, repo_root, branch, head):
        self.root = repo_root
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

    def merge_pr(self, number, head, *, commit_message=None):
        self.calls.append(("merge_pr", number, head))
        self.merge_sha = merge_commit(self.root, head)
        self.pr = {**self._pr(number, "merged", head), "merge_sha": self.merge_sha}
        return self.pr
