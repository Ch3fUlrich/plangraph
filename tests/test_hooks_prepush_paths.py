"""The pre-push hook has a lane path (no tests) and a train path (manifest, no tests) (LAND-PUSH-GATE-FAST-ONLY)."""
import json
import runpy

import pytest

from test_hooks_prepush import HOOK, commit_file, fake_ip, git, push_line, repo, run_hook  # noqa: F401

LOG_PREFLIGHT = ("import json, os, sys\n"
                 "from pathlib import Path\n"
                 "Path(os.environ['TEST_PREFLIGHT_MARKER']).write_text(json.dumps(sys.argv[1:]))\n")


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
def test_commit_policy_still_refuses_on_both_paths(repo, tmp_path, ref):
    r, base = repo
    install_preflight(r, tmp_path / "unused")
    base = git(r, "rev-parse", "HEAD").strip()
    git(r, "update-ref", "refs/remotes/origin/main", base)
    git(r, "checkout", "-q", "-b", "part", base)
    part = commit_file(r, "app/p.py", "p = 1\n")
    git(r, "checkout", "-q", "-b", "train", base)
    git(r, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "merge", "--no-ff", "-q", "part", "-m",
        "a merge with no trailers at all")
    git(r, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "--allow-empty", "-m", "no trailers")
    head = git(r, "rev-parse", "HEAD").strip()
    res = run_hook(r, [push_line(ref, head, "0" * 40)],
                   {"TEST_PREFLIGHT_MARKER": str(tmp_path / "argv"), "PLANGRAPH_TRAIN_MANIFEST": str(_one(tmp_path, part))})
    assert res.returncode == 1, res.stdout + res.stderr
    assert not (tmp_path / "argv").exists()
