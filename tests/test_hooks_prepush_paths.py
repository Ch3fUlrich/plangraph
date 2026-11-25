"""The pre-push hook has a lane path (no tests) and a train path (manifest, no tests) (LAND-PUSH-GATE-FAST-ONLY)."""
import json
import os
import runpy
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest

from merge_batch_env import train_cli  # noqa: F401
from test_hooks_prepush import HOOK, ZERO, commit_file, fake_ip, git, push_line, repo, run_hook  # noqa: F401

LOG_PREFLIGHT = ("import json, os, sys\n"
                 "from pathlib import Path\n"
                 "Path(os.environ['TEST_PREFLIGHT_MARKER']).write_text(json.dumps(sys.argv[1:]))\n")


def test_train_cli_uses_only_the_prebuilt_executable(tmp_path, monkeypatch, tmp_path_factory, pytestconfig):
    binary = tmp_path / "plangraph"
    binary.touch(mode=0o755)
    monkeypatch.setenv("PLANGRAPH_TEST_CLI", str(binary))
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("train_cli spawned a subprocess"))
    assert train_cli.__wrapped__(tmp_path_factory, pytestconfig) == binary


def test_train_cli_builds_once_across_ci_workers_without_prebuilt(tmp_path, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.delenv("PLANGRAPH_TEST_CLI", raising=False)
    monkeypatch.setenv("CI", "true")
    binary = tmp_path / "plangraph"
    binary.touch(mode=0o755)
    builds = []

    def run(command, **kwargs):
        assert command[:2] == ["cargo", "build"]
        assert command[command.index("-p") + 1] == "plangraph-cli"
        builds.append(command)
        artifact = {"reason": "compiler-artifact", "target": {"name": "plangraph"},
                    "executable": str(binary)}
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(artifact))

    monkeypatch.setattr(subprocess, "run", run)
    for name in ("gw0", "gw1"):
        base = tmp_path / name
        base.mkdir()
        config = SimpleNamespace(workerinput={"workerid": name})
        factory = SimpleNamespace(getbasetemp=lambda: base)
        assert train_cli.__wrapped__(factory, config) == binary
    assert len(builds) == 1


def test_preflight_builds_train_cli_once_before_affected_tests(repo, monkeypatch, tools_importable):
    import preflight

    root, base = repo
    selected = "tests/test_hooks_prepush_paths.py"
    commit_file(root, selected, "def test_train(): pass\n")
    binary = root / "plangraph"
    binary.touch(mode=0o755)
    calls = []

    def run(command, cwd, env):
        if command[:2] == ["cargo", "build"]:
            assert command[command.index("-p") + 1] == "plangraph-cli"
            calls.append("build")
            return 0, json.dumps({"reason": "compiler-artifact", "executable": str(binary),
                                  "target": {"name": "plangraph", "kind": ["bin"]}})
        if preflight.is_affected_step(command):
            calls.append("tests")
            assert preflight.check_timeout(command, cwd, env) == 240
            assert Path(env["PLANGRAPH_TEST_CLI"]).is_file()
        return 0, ""

    monkeypatch.setattr(preflight, "run", run)
    monkeypatch.setattr(preflight, "rust_commands", lambda *a: [])
    monkeypatch.setattr(preflight, "trim_shared_target", lambda _: None)
    monkeypatch.setenv("PREFLIGHT_TEST_WORKERS", "2")
    assert preflight.main(["--base", base], root) == 0
    assert calls == ["build", "tests"]


@pytest.fixture
def train_validator_on_path(train_cli, monkeypatch, tmp_path):
    bin_dir = tmp_path / "train-bin"
    bin_dir.mkdir()
    (bin_dir / "plangraph").symlink_to(train_cli)
    monkeypatch.setenv("PATH", str(bin_dir), prepend=os.pathsep)


def install_preflight(r, marker):
    commit_file(r, "tools/preflight.py", LOG_PREFLIGHT)
    return {"TEST_PREFLIGHT_MARKER": str(marker)}


def make_train(r, base, parts=2):
    """origin/main, then one merge commit per part: the train. Returns (train head, part heads)."""
    heads = []
    for i in range(parts):
        git(r, "checkout", "-q", "-b", f"part{i}", base)
        heads.append(commit_file(r, f"app/part{i}.py", f"part = {i}\n"))
    git(r, "checkout", "-q", "-b", "train", base)
    for i in range(parts):
        git(r, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
            "merge", "--no-ff", "-q", f"part{i}", "-m", f"part {i}\n\nPlan-Item: plangraph:TEST\nWriter: codex/test")
    return git(r, "rev-parse", "HEAD").strip(), heads


def manifest(tmp_path, heads):
    path = tmp_path / "manifest.txt"
    path.write_text("# train manifest\n" + "\n".join(heads) + "\n")
    return {"PLANGRAPH_TRAIN_MANIFEST": str(path)}


def test_a_lane_push_runs_no_preflight_and_no_tests(repo, tmp_path):
    """A lane/* ref is scrub, commit gate and stamp only (WRITER-PUSHES-EACH-ITEM-COMMIT); preflight is for trains."""
    r, base = repo
    env = install_preflight(r, tmp_path / "argv")
    # a failing unit test in the pushed tree: the lane hook does not run it
    head = commit_file(r, "tests/test_broken.py", "def test_broken():\n    assert False\n")
    commit = add_receipt(r, head)
    res = run_hook(r, [push_line("lane/x", head, base), receipt_line(commit, head, "x")],
                   {**env, **stub_plangraph(tmp_path, green=True)[0]})
    assert res.returncode == 0, res.stdout + res.stderr
    assert not (tmp_path / "argv").exists(), "preflight ran for a lane push"


@pytest.mark.usefixtures("train_validator_on_path")
def test_a_train_push_with_its_exact_manifest_passes_without_tests(repo, tmp_path):
    r, base = repo
    install_preflight(r, tmp_path / "unused")
    base = git(r, "rev-parse", "HEAD").strip()
    git(r, "update-ref", "refs/remotes/origin/main", base)
    env = {"TEST_PREFLIGHT_MARKER": str(tmp_path / "argv")}
    train, heads = make_train(r, base)
    res = run_hook(r, [push_line("land/train-20261121-3", train, "0" * 40)], {**env, **manifest(tmp_path, heads)})
    assert res.returncode == 0, res.stdout + res.stderr
    assert "--lane" in json.loads((tmp_path / "argv").read_text())
    assert "pre-push: train path" in res.stderr


def test_a_train_push_names_the_missing_plangraph_binary(repo, tmp_path):
    r, base = repo
    train, heads = make_train(r, base)
    env = manifest(tmp_path, heads)
    # Give the hook a PATH without the train CLI while retaining Git and Python.
    bin_dir = tmp_path / "hook-bin"
    bin_dir.mkdir()
    for name in ("git", "python3"):
        (bin_dir / name).symlink_to(shutil.which(name))
    env.update(PATH=str(bin_dir), HOME=str(tmp_path / "empty-home"))
    result = run_hook(r, [push_line("land/train-9", train, "0" * 40)], env)
    assert result.returncode == 1
    assert "plangraph binary not found on PATH" in result.stderr


@pytest.mark.usefixtures("train_validator_on_path")
def test_a_train_push_with_a_parent_outside_its_manifest_is_refused(repo, tmp_path):
    r, base = repo
    install_preflight(r, tmp_path / "unused")
    base = git(r, "rev-parse", "HEAD").strip()
    git(r, "update-ref", "refs/remotes/origin/main", base)
    train, heads = make_train(r, base)
    res = run_hook(r, [push_line("land/train-20261121-3", train, "0" * 40)],
                   {"TEST_PREFLIGHT_MARKER": str(tmp_path / "argv"), **manifest(tmp_path, heads[:1])})
    assert res.returncode == 1
    assert "outside its manifest" in res.stderr
    assert not (tmp_path / "argv").exists()


@pytest.mark.parametrize("case", ["order", "extra-commit", "no-manifest", "bad-manifest"])
@pytest.mark.usefixtures("train_validator_on_path")
def test_a_train_push_that_is_not_exactly_the_manifest_is_refused(repo, tmp_path, case):
    r, base = repo
    install_preflight(r, tmp_path / "unused")
    base = git(r, "rev-parse", "HEAD").strip()
    git(r, "update-ref", "refs/remotes/origin/main", base)
    train, heads = make_train(r, base)
    env = {"TEST_PREFLIGHT_MARKER": str(tmp_path / "argv")}
    if case == "order":
        env.update(manifest(tmp_path, heads[::-1]))
    elif case == "extra-commit":
        train = commit_file(r, "app/extra.py", "extra = 1\n")
        env.update(manifest(tmp_path, heads))
    elif case == "bad-manifest":
        env.update(manifest(tmp_path, ["not-a-sha"]))
    res = run_hook(r, [push_line("land/train-20261121-3", train, "0" * 40)], env)
    assert res.returncode == 1, res.stdout + res.stderr
    assert "train" in res.stderr


def test_a_train_push_is_not_judged_by_the_open_pull_request_parent_check(repo, monkeypatch):
    import subprocess
    hook = runpy.run_path(str(HOOK))
    seen = []

    class Recording:
        TimeoutExpired = subprocess.TimeoutExpired
        CalledProcessError = subprocess.CalledProcessError

        @staticmethod
        def run(command, **kwargs):
            seen.append(command)
            return subprocess.run(command, **kwargs)

    monkeypatch.setitem(hook["commit_gate"].__globals__, "subprocess", Recording)
    r, base = repo
    monkeypatch.chdir(r)
    assert hook["commit_gate"](base, open_prs=False) == 0
    assert not any(command[0] == "gh" for command in seen)
    seen.clear()
    hook["commit_gate"](base)   # a lane push still asks about open pull requests
    assert any(command[0] == "gh" for command in seen)


@pytest.mark.parametrize("ref", ["lane/x", "land/train-20261121-3"])
@pytest.mark.usefixtures("train_validator_on_path")
def test_scrub_still_refuses_on_both_paths(repo, tmp_path, ref):
    r, base = repo
    install_preflight(r, tmp_path / "unused")
    base = git(r, "rev-parse", "HEAD").strip()
    git(r, "update-ref", "refs/remotes/origin/main", base)
    git(r, "checkout", "-q", "-b", "part", base)
    part = commit_file(r, "app/net.py", f'url = "{fake_ip()}"\n')
    git(r, "checkout", "-q", "-b", "train", base)
    git(r, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "merge", "--no-ff", "-q", "part", "-m",
        "p\n\nPlan-Item: plangraph:TEST\nWriter: codex/test")
    head = git(r, "rev-parse", "HEAD").strip()
    lane_env, lane_lines = lane_extras(r, head, tmp_path, ref)
    res = run_hook(r, [push_line(ref, head, "0" * 40), *lane_lines],
                   {"TEST_PREFLIGHT_MARKER": str(tmp_path / "argv"), "PLANGRAPH_TRAIN_MANIFEST": str(_one(tmp_path, part)),
                    **lane_env})
    assert res.returncode == 1
    assert "app/net.py:1: ipv4" in res.stdout + res.stderr
    assert not (tmp_path / "argv").exists()


def _one(tmp_path, head):
    path = tmp_path / "m.txt"
    path.write_text(head + "\n")
    return path


@pytest.mark.parametrize("ref", ["lane/x", "land/train-20261121-3"])
@pytest.mark.usefixtures("train_validator_on_path")
def test_commit_policy_still_refuses_on_both_paths(repo, tmp_path, ref):
    """A valid one-merge train whose part commit lacks the trailers: the manifest passes, commit policy refuses."""
    r, base = repo
    install_preflight(r, tmp_path / "unused")
    base = git(r, "rev-parse", "HEAD").strip()
    git(r, "update-ref", "refs/remotes/origin/main", base)
    git(r, "checkout", "-q", "-b", "part", base)
    (r / "app/p.py").write_text("p = 1\n")
    git(r, "add", "-A")
    git(r, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "part without trailers")
    part = git(r, "rev-parse", "HEAD").strip()
    git(r, "checkout", "-q", "-b", "train", base)
    git(r, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "merge", "--no-ff", "-q", "part", "-m",
        "merge the part")
    head = git(r, "rev-parse", "HEAD").strip()
    lane_env, lane_lines = lane_extras(r, head, tmp_path, ref)
    res = run_hook(r, [push_line(ref, head, "0" * 40), *lane_lines],
                   {"TEST_PREFLIGHT_MARKER": str(tmp_path / "argv"), "PLANGRAPH_TRAIN_MANIFEST": str(_one(tmp_path, part)),
                    **lane_env})
    assert res.returncode == 1, res.stdout + res.stderr
    assert "missing Writer trailer" in res.stdout + res.stderr
    assert "outside its manifest" not in res.stderr and "out of manifest order" not in res.stderr
    assert not (tmp_path / "argv").exists()


@pytest.mark.parametrize("ref,lane", [("refs/heads/lane/x", True), ("refs/heads/main", False), ("refs/tags/v9", False)])
@pytest.mark.usefixtures("train_validator_on_path")
def test_only_lane_and_train_pushes_get_the_fast_only_mode(repo, tmp_path, ref, lane):
    """A lane ref skips preflight altogether; main and tags run it without --lane."""
    r, base = repo
    env = {**install_preflight(r, tmp_path / "argv"), **stub_plangraph(tmp_path, green=True)[0]}
    head = commit_file(r, "app/other.py", "y = 2\n")
    line = f"{ref} {head} {ref} {base}" if ref != "refs/tags/v9" else f"{ref} {head} {ref} {'0' * 40}"
    lines = [line]
    if lane:
        lines.append(receipt_line(add_receipt(r, head), head, "x"))
    res = run_hook(r, lines, env)
    assert res.returncode == 0, res.stdout + res.stderr
    if lane:
        assert not (tmp_path / "argv").exists()
    else:
        assert "--lane" not in json.loads((tmp_path / "argv").read_text())


@pytest.mark.parametrize("extra", [None, "unrelated", "no-harness", "drop-no-harness", "foreign-parent"])
@pytest.mark.usefixtures("train_validator_on_path")
def test_a_train_push_permits_only_attributed_resolves_and_drops(repo, tmp_path, extra):
    r, _ = repo
    env = install_preflight(r, tmp_path / "argv")
    base = git(r, "rev-parse", "HEAD").strip()
    git(r, "update-ref", "refs/remotes/origin/main", base)
    _, parts = make_train(r, base, parts=3)

    def commit(message):
        git(r, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
            "commit", "--allow-empty", "-qm", message)

    trailers = "Writer: codex/train-resolver\nHarness: codex"
    commit(f"Train 9: resolve first issue\n\n{trailers}")
    commit(f"Train 9: resolve second issue\n\n{trailers}")
    merge = git(r, "rev-parse", "HEAD~2").strip()
    git(r, "revert", "--no-commit", "-m", "1", merge)
    drop_trailers = trailers if extra != "drop-no-harness" else trailers.splitlines()[0]
    commit(f"Train 9: drop last part\n\nThis reverts commit {merge}.\n\nDropped-Part: plangraph:TEST\n{drop_trailers}")
    if extra == "unrelated":
        commit(f"Unrelated\n\n{trailers}")
    elif extra == "no-harness":
        commit("Train 9: resolve without a harness\n\nWriter: codex/train-resolver")
    elif extra == "foreign-parent":
        git(r, "checkout", "-qb", "foreign", base)
        commit(f"Foreign work\n\n{trailers}")
        git(r, "checkout", "-q", "train")
        git(r, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
            "merge", "-q", "--no-ff", "foreign", "-m", "Foreign merge")
    head = git(r, "rev-parse", "HEAD").strip()
    result = run_hook(r, [push_line("land/train-9", head, "0" * 40)], {**env, **manifest(tmp_path, parts)})
    if extra is None:
        assert result.returncode == 0, result.stdout + result.stderr
        assert "manifest ok" in result.stderr and (tmp_path / "argv").exists()
    else:
        assert result.returncode == 1 and not (tmp_path / "argv").exists()
        assert ("outside its manifest" if extra == "foreign-parent" else "extra commit") in result.stderr
        if extra in ("no-harness", "drop-no-harness"):
            assert f"train commit {head} is an extra commit without a valid Harness trailer" in result.stderr


# WRITER-PUSHES-EACH-ITEM-COMMIT / LE-FAST-CHECKS-RESULT-READ-FROM-TAIL: a lane/* push is scrub, commit gate and stamp only.
BOUND_SECONDS = 20


def stub_plangraph(tmp_path, green):
    """A `plangraph` on PATH that answers the receipt questions; the log shows what the hook asked.
    `--receipt-ref` answers like the real mapping for plain lane names; the others exit by `green`."""
    directory = tmp_path / "bin"
    directory.mkdir(exist_ok=True)
    log = tmp_path / "plangraph.log"
    tool = directory / "plangraph"
    tool.write_text(
        f'#!/bin/sh\necho "$@" >> {log}\n'
        'if [ "$2" = "--receipt-ref" ]; then\n'
        '  echo "refs/notes/plangraph-fast-checks-$(echo "${3#refs/heads/}" | sed \'s|/|%2f|g\')"; exit 0\n'
        f'fi\nexit {0 if green else 1}\n')
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    return {"PATH": f"{directory}{os.pathsep}{os.environ['PATH']}"}, log


def lane_line(sha, remote=ZERO, name="pr10"):
    return f"refs/heads/lane/{name} {sha} refs/heads/lane/{name} {remote}"


def receipt_text(head, verdict="green", rc=0, label="fmt"):
    return f"head {head}\nresult {verdict}\ncheck {label} rc={rc} wall=1.000\n"


def receipt_commit(repo, head, text=None, parent=None, message="fast-checks receipt", author="plangraph"):
    """The receipt of `head` as `fast-checks` publishes it: one parentless commit, one file named by the head."""
    def plumbing(*args, stdin=None):
        out = subprocess.run(["git", *args], cwd=str(repo), input=stdin, capture_output=True, text=True, check=True,
                             env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"})
        return out.stdout.strip()

    blob = plumbing("hash-object", "-w", "--stdin", stdin=text or receipt_text(head))
    tree = plumbing("mktree", stdin=f"100644 blob {blob}\t{head}\n")
    parents = ["-p", parent] if parent else []
    commit = plumbing("-c", f"user.name={author}", "-c", "user.email=plangraph@localhost", "commit-tree", tree,
                      *parents, "-m", message)
    plumbing("update-ref", f"refs/plangraph/receipts/{head}", commit)
    return commit


def receipt_ref(name):
    return f"refs/notes/plangraph-fast-checks-lane%2f{name}"


def receipt_line(commit, head, name="pr10"):
    """The push record of a lane's receipt ref (what `git push` sends next to the lane head)."""
    return f"refs/plangraph/receipts/{head} {commit} {receipt_ref(name)} {ZERO}"


def add_receipt(repo, head, text=None):
    return receipt_commit(repo, head, text)


def lane_push(repo, head, name="pr10", text=None):
    """A lane head and its receipt ref, as one atomic push presents them to the hook."""
    commit = receipt_commit(repo, head, text)
    return [lane_line(head, name=name), receipt_line(commit, head, name)]


def lane_extras(r, head, tmp_path, ref):
    """A lane ref needs a receipt ref in its push and a green reader on PATH; other refs need neither."""
    if not ref.startswith(("lane/", "refs/heads/lane/")):
        return {}, []
    return stub_plangraph(tmp_path, green=True)[0], [receipt_line(add_receipt(r, head), head, "x")]


def with_tripwire_preflight(r):
    """A pushed tree whose preflight would leave a mark: a lane push must never run it."""
    mark = r.parent / "preflight-ran"
    return commit_file(r, "tools/preflight.py",
                       "import os, pathlib\npathlib.Path(os.environ['PREFLIGHT_MARK']).write_text('ran')\n"), mark


def test_lane_push_with_a_green_receipt_runs_no_preflight_and_stays_fast(repo, tmp_path):
    r, _ = repo
    head, mark = with_tripwire_preflight(r)
    env, log = stub_plangraph(tmp_path, green=True)
    start = time.monotonic()
    res = run_hook(r, lane_push(r, head), {**env, "PREFLIGHT_MARK": str(mark)})
    elapsed = time.monotonic() - start
    assert res.returncode == 0, res.stderr
    assert not mark.exists(), "preflight ran for a lane push"
    asked = log.read_text()
    assert "fast-checks --check-notes" in asked
    assert "fast-checks --receipt-ref refs/heads/lane/pr10" in asked
    assert f"fast-checks --stamp-green {head} --notes-commit" in asked
    assert elapsed < BOUND_SECONDS, f"lane push took {elapsed:.1f}s"
    assert git(r, "worktree", "list").count("\n") == 1, "the hook left a worktree"


def test_lane_push_without_its_receipt_ref_or_with_a_red_one_is_refused(repo, tmp_path):
    r, _ = repo
    head = commit_file(r, "app/lane.txt", "clean\n")
    env, _ = stub_plangraph(tmp_path, green=True)
    # A local stamp alone publishes nothing: the receipt must be part of the push.
    res = run_hook(r, [lane_line(head)], env)
    assert res.returncode == 1
    assert "no green fast-checks receipt" in res.stderr
    red_env, _ = stub_plangraph(tmp_path, green=False)
    res = run_hook(r, lane_push(r, head), red_env)
    assert res.returncode == 1


def test_the_old_unstamped_flag_no_longer_bypasses_the_receipt(repo, tmp_path):
    r, _ = repo
    head = commit_file(r, "app/lane.txt", "clean\n")
    env, _ = stub_plangraph(tmp_path, green=False)
    res = run_hook(r, [lane_line(head)], {**env, "PG_LANE_PUSH_UNSTAMPED": "1"})
    assert res.returncode == 1
    assert "no green fast-checks receipt" in res.stderr


@pytest.mark.usefixtures("train_validator_on_path")
def test_the_real_receipt_reader_validates_what_a_receipt_ref_publishes(repo, tmp_path):
    r, _ = repo
    head = commit_file(r, "app/lane.txt", "clean\n")
    # A valid green receipt of the pushed head, in the lane's designated ref, passes.
    assert run_hook(r, lane_push(r, head), {}).returncode == 0
    # An ordinary commit is not a receipt ref.
    res = run_hook(r, [lane_line(head), f"refs/heads/x {head} {receipt_ref('pr10')} {ZERO}"], {})
    assert res.returncode == 1
    assert "not a receipt ref" in res.stderr
    # A red receipt does not publish the head.
    assert run_hook(r, lane_push(r, head, text=receipt_text(head, "red", 1)), {}).returncode == 1
    # A receipt of another head does not publish this one.
    other = git(r, "rev-parse", "HEAD~1").strip()
    res = run_hook(r, [lane_line(head), receipt_line(receipt_commit(r, other), other)], {})
    assert res.returncode == 1
    assert "no green fast-checks receipt" in res.stderr


@pytest.mark.usefixtures("train_validator_on_path")
def test_content_that_only_a_parent_of_the_receipt_commit_holds_is_never_published(repo, tmp_path):
    r, _ = repo
    head = commit_file(r, "app/lane.txt", "clean\n")
    old = receipt_commit(r, head, receipt_text(head, label=fake_ip()))
    child = receipt_commit(r, head, parent=old)  # the tip tree is clean; the parent is not
    res = run_hook(r, [lane_line(head), receipt_line(child, head)], {})
    assert res.returncode == 1
    assert "has parents" in res.stderr


@pytest.mark.usefixtures("train_validator_on_path")
def test_a_receipt_ref_travels_only_as_the_designated_ref_of_a_lane_head_in_the_same_push(repo, tmp_path):
    r, _ = repo
    head = commit_file(r, "app/lane.txt", "clean\n")
    commit = receipt_commit(r, head)
    # A receipt-only push (no lane head with it) is refused.
    res = run_hook(r, [receipt_line(commit, head)], {})
    assert res.returncode == 1
    assert "not the receipt ref of a lane head" in res.stderr
    # Another lane's receipt ref does not publish this lane.
    res = run_hook(r, [lane_line(head, name="pr10"), receipt_line(commit, head, name="other")], {})
    assert res.returncode == 1


@pytest.mark.usefixtures("train_validator_on_path")
def test_the_receipt_commit_object_itself_may_carry_no_text(repo, tmp_path):
    r, _ = repo
    head = commit_file(r, "app/lane.txt", "clean\n")
    # The tree is a clean receipt; the message or the author carries the text the file scrub never sees.
    for odd in ({"message": f"reviewed on {fake_ip()}"}, {"author": "someone private"}):
        commit = receipt_commit(r, head, **odd)
        res = run_hook(r, [lane_line(head), receipt_line(commit, head)], {})
        assert res.returncode == 1, odd
        assert "not a receipt ref" in res.stderr and "canonical" in res.stderr, res.stderr


@pytest.mark.usefixtures("train_validator_on_path")
def test_a_phase_push_has_no_designated_receipt_so_a_receipt_ref_with_it_is_refused(repo, tmp_path):
    r, base = repo
    install_preflight(r, tmp_path / "argv")
    head = commit_file(r, "app/phase.txt", "clean\n")
    commit = receipt_commit(r, head)
    line = f"refs/heads/phase/x {head} refs/heads/phase/x {base}"
    res = run_hook(r, [line, receipt_line(commit, head, "x")], {"TEST_PREFLIGHT_MARKER": str(tmp_path / "argv")})
    assert res.returncode == 1
    assert "not the receipt ref of a lane head" in res.stderr


@pytest.mark.usefixtures("train_validator_on_path")
def test_a_receipt_ref_is_scrubbed_like_any_pushed_content(repo, tmp_path):
    r, _ = repo
    head = commit_file(r, "app/lane.txt", "clean\n")
    # Well formed, but a check label that is an address: the public scrub refuses the blob.
    text = receipt_text(head, label=fake_ip())
    res = run_hook(r, lane_push(r, head, text=text), {})
    assert res.returncode == 1
    assert "scrub hit" in res.stderr


def test_lane_push_still_scrubs_and_runs_the_commit_gate(repo, tmp_path):
    r, _ = repo
    env, _ = stub_plangraph(tmp_path, green=True)
    hit = commit_file(r, "app/lane.txt", f"ip = {fake_ip()}\n")
    assert run_hook(r, lane_push(r, hit), env).returncode == 1
    git(r, "reset", "-q", "--hard", "HEAD~1")
    git(r, "commit", "-q", "--allow-empty", "-m", "no trailers")
    bare = git(r, "rev-parse", "HEAD").strip()
    res = run_hook(r, lane_push(r, bare), env)
    assert res.returncode == 1
    assert "Plan-Item" in res.stderr or "Writer" in res.stderr


def test_concurrent_pushes_from_two_worktrees_leave_both_worktrees_in_place(repo, tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    r, base = repo
    trees = []
    for name in ("one", "two"):
        tree = tmp_path / f"wt-{name}"
        git(r, "worktree", "add", "-q", "-b", f"lane/{name}", str(tree), base)
        git(tree, "config", "user.name", "t")
        git(tree, "config", "user.email", "t@example.invalid")
        head = commit_file(tree, f"app/{name}.txt", "clean\n")
        trees.append((name, tree, lane_push(tree, head, name=name)))
    env, _ = stub_plangraph(tmp_path, green=True)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda t: run_hook(t[1], t[2], env), trees))
    for result in results:
        assert result.returncode == 0, result.stderr
    assert all(tree.is_dir() for _, tree, _ in trees)
    assert git(r, "worktree", "list", "--porcelain").count("worktree ") == 3
