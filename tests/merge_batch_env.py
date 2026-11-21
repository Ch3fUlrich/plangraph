"""Hermetic fixtures for tests/test_merge_batch.py (D-852): temp bare origin + clone, stub gh.

PATH holds ONLY the stub gh and a bin dir with git, sh, env and python3, so a real gh (or any
other live tool) is unreachable; the tool under test runs as a subprocess with a minimal env.
"""
import json
from contextlib import contextmanager
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "merge_batch.py"
STUB = Path(__file__).with_name("merge_batch_gh_stub.py")
#: A `git` that interferes with the n-th `git $CRASH_SUB` it is asked to run (see Env.run): kill the parent
#: (the tool) right after it, like an OOM kill; fail after it ran (its side effect stands); refuse it (exit
#: non-zero without running, so it leaves no side effect behind).
CRASH_GIT = """#!/bin/sh
sub=
skip=0
for arg in "$@"; do
  if [ "$skip" = 1 ]; then skip=0; continue; fi
  case "$arg" in -c) skip=1 ;; -*) ;; *) sub=$arg; break ;; esac
done
hit=0
if [ "$sub" = "$CRASH_SUB" ]; then
  n=0
  [ -f "$CRASH_COUNT" ] && read n < "$CRASH_COUNT"
  n=$((n + 1))
  echo "$n" > "$CRASH_COUNT"
  [ "$n" = "$CRASH_NTH" ] && hit=1
fi
if [ "$hit" = 1 ] && [ "$CRASH_MODE" = "refuse" ]; then
  exit 128
fi
"$REAL_GIT" "$@"
rc=$?
if [ "$hit" = 1 ]; then
  if [ "$CRASH_MODE" = "fail" ]; then
    exit 128
  fi
  kill -9 "$PPID"
fi
exit $rc
"""
ROLLUP = {
    "green": [{"__typename": "CheckRun", "name": "ci-gate", "status": "COMPLETED", "conclusion": "SUCCESS"}],
    "red": [{"__typename": "CheckRun", "name": "ci-gate", "status": "COMPLETED", "conclusion": "FAILURE"}],
    "pending": [{"__typename": "CheckRun", "name": "ci-gate", "status": "IN_PROGRESS", "conclusion": ""}],
    "none": [],
}


def pr_json(number, ref, base="main", title=None, head=None, labels=("batch-ready",), draft=False,
            ci="green", files=(), lines=10, cross=False, state="OPEN", changed=None):
    """One PR as `gh pr view --json` returns it."""
    return {"number": number, "title": title or f"change {number}", "headRefName": ref,
            "headRefOid": head or f"{number:040x}", "baseRefName": base, "state": state, "isDraft": draft,
            "isCrossRepository": cross, "labels": [{"name": name} for name in labels],
            "files": [{"path": path, "additions": 0, "deletions": 0} for path in files],
            "changedFiles": len(files) if changed is None else changed, "additions": lines, "deletions": 0,
            "statusCheckRollup": ROLLUP[ci], "mergeCommit": None}


MAIN_STATE_STUB = """#!/usr/bin/env python3
import json, os, sys
state = json.load(open(os.path.join(os.environ["GH_STUB_DIR"], "state.json"))).get("main_state", "green")
args = sys.argv[1:]
exception = args[args.index("--exception") + 1] if "--exception" in args else None
if args[:1] != ["main-state"] or "--admit" not in args:
    sys.exit(2)
if state == "green" or (state == "red" and exception == "EXCEPTION:main-red-fix"):
    sys.exit(0)
print("main_" + state if state in ("red", "pending") else state, file=sys.stderr)
sys.exit(3)
"""


class Env:
    def __init__(self, tmp_path):
        self.tmp, self.origin, self.repo = tmp_path, tmp_path / "origin.git", tmp_path / "repo"
        self.stubs, self.bin, self.home = tmp_path / "stubs", tmp_path / "bin", tmp_path / "home"
        for path in (self.stubs, self.bin, self.home):
            path.mkdir()
        shutil.copy(STUB, self.stubs / "gh")
        (self.stubs / "gh").chmod(0o755)
        # `plangraph main-state --admit` as the tool calls it: state.json "main_state" is green, red, pending ...
        # and the exception passes only while it is red (the real function is tested in Rust).
        (self.stubs / "main-state").write_text(MAIN_STATE_STUB)
        (self.stubs / "main-state").chmod(0o755)
        for tool in ("git", "sh", "env"):
            (self.bin / tool).symlink_to(shutil.which(tool))
        (self.bin / "python3").symlink_to(sys.executable)
        # pg-policy: a stub that lets every part in; tests point PG_POLICY_BIN at a refusing one.
        (self.stubs / "pg-policy").write_text("#!/bin/sh\nexit 0\n")
        (self.stubs / "pg-policy").chmod(0o755)
        self.env = {"PATH": f"{self.stubs}:{self.bin}", "HOME": str(self.home), "GH_STUB_DIR": str(self.stubs),
                    "PLANGRAPH_BIN": str(self.stubs / "main-state"),
                    "PG_POLICY_BIN": str(self.stubs / "pg-policy"),
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

    def attach_seat(self, batch, rec):
        config = self.home / ".config" / "plangraph" / "keeper.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        mirror = self.tmp / "mirror"
        mirror.mkdir(mode=0o700, exist_ok=True)
        (mirror / "fixture").mkdir(mode=0o700, exist_ok=True)
        config.write_text(json.dumps({"spawner_records_dir": str(mirror)}))
        config.chmod(0o600)
        for index, key in enumerate(rec["items"]):
            run_id = f"run-part-{rec['pr']}-{index}"
            path = mirror / "fixture" / run_id / "record.json"
            for directory in (mirror, mirror / "fixture", path.parent):
                directory.mkdir(exist_ok=True)
                directory.chmod(0o700)
            path.write_text(json.dumps({
                "run_id": run_id, "host": "fixture", "item_key": key,
                "role": "review", "status": "completed", "exit_code": 0,
                "served_model": "gemini-test", "finish_reason": "stop", "attempts": [{}],
                "head_sha": rec["head"], "completion": {"head_sha": rec["head"]},
                "stdout_tail": "TEST_GAMING: no\nVERDICT: PASS\n",
            }))
            path.chmod(0o600)
            result = self.run("review", "--name", batch, "--pr", str(rec["pr"]), "--run-id", run_id)
            assert result.returncode == 0, result.stderr

    @contextmanager
    def hook_readiness(self, monkeypatch):
        """Start the push deadline only after both fake hook workers report ready."""
        import os
        import select

        ready = self.tmp / 'hook-ready'
        os.mkfifo(ready)
        fd = os.open(ready, os.O_RDWR)
        original = subprocess.Popen

        class ReadyPush(original):
            def communicate(process, *args, **kwargs):
                if process.args[:2] == ['git', 'push'] and not getattr(process, 'hook_ready', False):
                    data = b''
                    while len(data) < 2:
                        assert select.select([fd], [], [], 10)[0], 'fake hook workers did not start'
                        data += os.read(fd, 2 - len(data))
                    process.hook_ready = True
                return super().communicate(*args, **kwargs)

        try:
            with monkeypatch.context() as patch:
                patch.setattr(subprocess, 'Popen', ReadyPush)
                yield
        finally:
            os.close(fd)

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

    def fresh_clone(self, name="fresh"):
        """A clone that has only main: the PR heads are not in it until the tool fetches them."""
        path = self.tmp / name
        self.git("clone", "-q", "--no-local", "--single-branch", "--branch", "main", str(self.origin), str(path), cwd=self.tmp)
        return path

    def has_commit(self, repo, sha):
        proc = subprocess.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=repo, env=self.env, capture_output=True)
        return proc.returncode == 0

    def seed(self, files):
        """Commit ``files`` (path -> text) to main in the origin."""
        for rel, text in files.items():
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / rel).write_text(text)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "seed more")
        self.git("push", "-q", "origin", "main")

    def branch_pr(self, number, files, *, base="main", ref=None, item=None, moves=None, trailer="", **kw):
        """A real PR branch in the origin (plus refs/pull/N/head) and its canned gh record. ``moves`` renames
        files (old -> new); gh lists only the new path of a rename."""
        ref = ref or f"lane/p{number}"
        self.git("checkout", "-q", "-B", ref, f"origin/{base}")
        for old, new in (moves or {}).items():
            (self.repo / new).parent.mkdir(parents=True, exist_ok=True)
            self.git("mv", old, new)
        for rel, text in files.items():
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / rel).write_text(text)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", f"change {number}" + (f"\n\nPlan-Item: plangraph:{item}" if item else "") + trailer)
        sha = self.git("rev-parse", "HEAD")
        self.git("push", "-q", "origin", ref, f"{sha}:refs/pull/{number}/head")
        self.git("checkout", "-q", "main")
        self.add_pr(number, ref, base=base, head=sha, files=[*files, *(moves or {}).values()], **kw)
        return sha

    def push_commit(self, number, files):
        """A new commit on a PR's branch, pushed after a batch was built: the PR head moves, as on GitHub."""
        self.load()
        pr = self.state["prs"][str(number)]
        self.git("checkout", "-q", pr["headRefName"])
        for rel, text in files.items():
            (self.repo / rel).write_text(text)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "late change")
        sha = self.git("rev-parse", "HEAD")
        self.git("push", "-q", "origin", pr["headRefName"], f"{sha}:refs/pull/{number}/head")
        self.git("checkout", "-q", "main")
        pr["headRefOid"] = sha
        self.save()
        return sha

    def run(self, *argv, cwd=None, crash=None, tool=None):
        """Run the tool. ``crash=(git subcommand, n[, mode])`` interferes with the tool's n-th
        `git <subcommand>`: mode ``kill`` (the default) kills the tool right after it ran (a crash window),
        ``fail`` runs it (its side effect stands) and then makes git exit non-zero, ``refuse`` makes git
        exit non-zero without running it. ``tool`` runs another copy of merge_batch.py."""
        env = dict(self.env)
        if crash:
            hook, count = self.tmp / "crashbin", self.tmp / "crash.count"
            hook.mkdir(exist_ok=True)
            (hook / "git").write_text(CRASH_GIT)
            (hook / "git").chmod(0o755)
            count.unlink(missing_ok=True)
            env.update(PATH=f"{hook}:{env['PATH']}", REAL_GIT=shutil.which("git"), CRASH_SUB=crash[0],
                       CRASH_NTH=str(crash[1]), CRASH_COUNT=str(count))
            if len(crash) > 2:
                env["CRASH_MODE"] = crash[2]
        return subprocess.run([sys.executable, str(tool or TOOL), *argv], cwd=cwd or self.repo, env=env,
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


# --- helpers shared by the test modules ------------------------------------------------------------
SHARED = "".join(f"line {i}\n" for i in range(1, 7))


def env_fixture(tmp_path):
    """The body of every module's ``env`` fixture: an Env, checked for stub calls nobody expected."""
    env = Env(tmp_path)
    yield env
    assert not (env.stubs / "UNEXPECTED").exists(), "the tool called a gh command the stub lacks"


def two(text):
    return SHARED.replace("line 3", text)


def conflicting(env):
    """PR 1 and 2 edit the same line, 3 is independent."""
    env.branch_pr(1, {"shared.txt": two("line 3 one")}, item="ITEM-A", title="edit one")
    env.branch_pr(2, {"shared.txt": two("line 3 two")}, item="ITEM-B")
    env.branch_pr(3, {"c.txt": "c\n"})


def first_parent(env, ref, cwd=None):
    return env.git("log", "--first-parent", "--format=%s", ref, cwd=cwd).splitlines()


def pushed(env, name="l1", prs=(1, 2, 3), checks="pass", plan=None):
    """A pushed batch of PRs 1..3 whose batch PR is #100 with green checks and a passing approval."""
    env.branch_pr(1, {"a.txt": "a\n"}, item="ITEM-A")
    env.branch_pr(2, {"b.txt": "b\n"}, item="ITEM-B")
    env.branch_pr(3, {"c.txt": "c\n"}, item="ITEM-C")
    assert env.run("build", "--name", name, "--prs", ",".join(map(str, prs)),
                   *(["--plan", plan] if plan else [])).returncode == 0
    assert env.run("push", "--name", name).stdout == "100\n"
    for rec in json.loads(env.state_file(name).read_text())["merged"]:
        env.attach_seat(name, rec)
    env.load()  # the stub created PR 100
    env.state["checks"]["100"] = [{"name": "ci-gate", "bucket": checks}]
    env.save()
    approval = env.tmp / "approval.txt"
    approval.write_text(f"looks fine\nHEAD: {head_of(env, name)}\nVERDICT: PASS\n")
    return approval


def head_of(env, name="l1"):
    return json.loads(env.state_file(name).read_text())["head"]


def land(env, approval, *extra, name="l1", number="100"):
    return env.run("land", "--name", name, "--batch-pr", number, "--approval", str(approval), "--wait", "0", *extra)
