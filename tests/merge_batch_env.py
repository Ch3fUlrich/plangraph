"""Hermetic fixtures for tests/test_merge_batch.py (D-852): temp bare origin + clone, stub gh.

PATH holds ONLY the stub gh and a bin dir with git, sh, env and python3, so a real gh (or any
other live tool) is unreachable; the tool under test runs as a subprocess with a minimal env.
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "merge_batch.py"
STUB = Path(__file__).with_name("merge_batch_gh_stub.py")
ROLLUP = {
    "green": [{"__typename": "CheckRun", "name": "ci-gate", "status": "COMPLETED", "conclusion": "SUCCESS"}],
    "red": [{"__typename": "CheckRun", "name": "ci-gate", "status": "COMPLETED", "conclusion": "FAILURE"}],
    "pending": [{"__typename": "CheckRun", "name": "ci-gate", "status": "IN_PROGRESS", "conclusion": ""}],
    "none": [],
}


def pr_json(number, ref, base="main", title=None, head=None, labels=("batch-ready",), draft=False,
            ci="green", files=(), lines=10, cross=False, state="OPEN"):
    """One PR as `gh pr view --json` returns it."""
    return {"number": number, "title": title or f"change {number}", "headRefName": ref,
            "headRefOid": head or f"{number:040x}", "baseRefName": base, "state": state, "isDraft": draft,
            "isCrossRepository": cross, "labels": [{"name": name} for name in labels],
            "files": [{"path": path, "additions": 0, "deletions": 0} for path in files],
            "additions": lines, "deletions": 0, "statusCheckRollup": ROLLUP[ci], "mergeCommit": None}


class Env:
    def __init__(self, tmp_path):
        self.tmp, self.origin, self.repo = tmp_path, tmp_path / "origin.git", tmp_path / "repo"
        self.stubs, self.bin, self.home = tmp_path / "stubs", tmp_path / "bin", tmp_path / "home"
        for path in (self.stubs, self.bin, self.home):
            path.mkdir()
        shutil.copy(STUB, self.stubs / "gh")
        (self.stubs / "gh").chmod(0o755)
        for tool in ("git", "sh", "env"):
            (self.bin / tool).symlink_to(shutil.which(tool))
        (self.bin / "python3").symlink_to(sys.executable)
        self.env = {"PATH": f"{self.stubs}:{self.bin}", "HOME": str(self.home), "GH_STUB_DIR": str(self.stubs),
                    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
                    "GIT_AUTHOR_NAME": "tester", "GIT_AUTHOR_EMAIL": "tester",
                    "GIT_COMMITTER_NAME": "tester", "GIT_COMMITTER_EMAIL": "tester", "LC_ALL": "C"}
        self.state = {"repo": "owner/proj", "default_branch": "main", "origin": str(self.origin), "prs": {},
                      "checks": {}, "labels": ["batch-ready"], "next_pr": 100, "no_auto_merge": []}
        self.git("init", "-q", "--bare", "-b", "main", str(self.origin), cwd=tmp_path)
        self.git("clone", "-q", str(self.origin), str(self.repo), cwd=tmp_path)
        self.git("symbolic-ref", "HEAD", "refs/heads/main")
        (self.repo / "shared.txt").write_text("".join(f"line {i}\n" for i in range(1, 7)))
        (self.repo / "README.md").write_text("demo\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "seed")
        self.git("push", "-q", "origin", "main")
        self.save()

    def git(self, *args, cwd=None):
        proc = subprocess.run(["git", *args], cwd=cwd or self.repo, env=self.env, capture_output=True, text=True)
        assert proc.returncode == 0, f"git {args}: {proc.stderr}"
        return proc.stdout.strip()

    def save(self):
        (self.stubs / "state.json").write_text(json.dumps(self.state))

    def load(self):
        self.state = json.loads((self.stubs / "state.json").read_text())
        return self.state

    def add_pr(self, number, ref, **kw):
        self.state["prs"][str(number)] = pr_json(number, ref, **kw)
        self.save()

    def branch_pr(self, number, files, *, base="main", ref=None, item=None, **kw):
        """A real PR branch in the origin (plus refs/pull/N/head) and its canned gh record."""
        ref = ref or f"lane/p{number}"
        self.git("checkout", "-q", "-B", ref, f"origin/{base}")
        for rel, text in files.items():
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / rel).write_text(text)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", f"change {number}" + (f"\n\nPlan-Item: plangraph:{item}" if item else ""))
        sha = self.git("rev-parse", "HEAD")
        self.git("push", "-q", "origin", ref, f"{sha}:refs/pull/{number}/head")
        self.git("checkout", "-q", "main")
        self.add_pr(number, ref, base=base, head=sha, files=list(files), **kw)
        return sha

    def run(self, *argv, cwd=None):
        return subprocess.run([sys.executable, str(TOOL), *argv], cwd=cwd or self.repo, env=self.env,
                              capture_output=True, text=True, timeout=120)

    def run_gh(self, *argv):
        """Call `gh` the way the tool does (through PATH, so the stub)."""
        return subprocess.run(["gh", *argv], cwd=self.repo, env=self.env, capture_output=True, text=True)

    def calls(self, *prefix):
        path = self.stubs / "calls.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [row for row in rows if row[:len(prefix)] == list(prefix)]

    def state_file(self, name):
        return self.repo / ".git" / "merge-batch" / f"{name}.json"
