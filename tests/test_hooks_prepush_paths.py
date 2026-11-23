"""The pre-push hook has a lane path (no tests) and a train path (manifest, no tests) (LAND-PUSH-GATE-FAST-ONLY)."""
import json
import os
import runpy
import shutil
import subprocess
from pathlib import Path

import pytest

from merge_batch_env import train_cli  # noqa: F401
from test_hooks_prepush import HOOK, commit_file, fake_ip, git, push_line, repo, run_hook  # noqa: F401

pytestmark = pytest.mark.long_test_deadline(
    360, reason="Hosted CI run 45260747564 was still building the shared train CLI at 120 s. "
    "A prior cold run took 141 s, and the hook subprocess has its own 120 s bound.",
)

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


def test_a_lane_push_runs_preflight_in_lane_mode_and_no_tests(repo, tmp_path):
    r, base = repo
    env = install_preflight(r, tmp_path / "argv")
    # a failing unit test in the pushed tree: the lane hook does not run it
    head = commit_file(r, "tests/test_broken.py", "def test_broken():\n    assert False\n")
    res = run_hook(r, [push_line("lane/x", head, base)], env)
    assert res.returncode == 0, res.stdout + res.stderr
    argv = json.loads((tmp_path / "argv").read_text())
    assert "--lane" in argv
    assert "--fast" not in argv or "--lane" in argv


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
    res = run_hook(r, [push_line(ref, head, "0" * 40)],
                   {"TEST_PREFLIGHT_MARKER": str(tmp_path / "argv"), "PLANGRAPH_TRAIN_MANIFEST": str(_one(tmp_path, part))})
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
    res = run_hook(r, [push_line(ref, head, "0" * 40)],
                   {"TEST_PREFLIGHT_MARKER": str(tmp_path / "argv"), "PLANGRAPH_TRAIN_MANIFEST": str(_one(tmp_path, part))})
    assert res.returncode == 1, res.stdout + res.stderr
    assert "missing Writer trailer" in res.stdout + res.stderr
    assert "outside its manifest" not in res.stderr and "out of manifest order" not in res.stderr
    assert not (tmp_path / "argv").exists()


@pytest.mark.parametrize("ref,lane", [("refs/heads/lane/x", True), ("refs/heads/main", False), ("refs/tags/v9", False)])
@pytest.mark.usefixtures("train_validator_on_path")
def test_only_lane_and_train_pushes_get_the_fast_only_mode(repo, tmp_path, ref, lane):
    r, base = repo
    env = install_preflight(r, tmp_path / "argv")
    head = commit_file(r, "app/other.py", "y = 2\n")
    line = f"{ref} {head} {ref} {base}" if ref != "refs/tags/v9" else f"{ref} {head} {ref} {'0' * 40}"
    res = run_hook(r, [line], env)
    assert res.returncode == 0, res.stdout + res.stderr
    assert ("--lane" in json.loads((tmp_path / "argv").read_text())) is lane


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
