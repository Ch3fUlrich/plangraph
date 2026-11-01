import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from git_identity import identity_args
from plangraph.spawner import git_guard
from plangraph.spawner.git_guard import check_guard, init_guard, writer_launch
from plangraph.spawner.runner import SpawnerRunner


class FakeProcess:
    pid = 1234

    def __init__(self, returncode: int = 0, stdout: bytes = b"", stderr: bytes = b""):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self):
        return self._stdout, self._stderr


def _setup_repo_and_worktree(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "core.autocrlf", "true"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", *identity_args(), "commit", "--allow-empty", "-m", "init"], cwd=repo, check=True, capture_output=True)

    wt = tmp_path / "worktree"
    subprocess.run(["git", "worktree", "add", str(wt), "-b", "wt-branch"], cwd=repo, check=True, capture_output=True)
    return repo, wt


def _job():
    return SimpleNamespace(status="completed", exit_code=0, events=[])


def _set(config: Path, key: str, value: str):
    subprocess.run(["git", "config", "--file", str(config), key, value], check=True, capture_output=True)


def _get(config: Path, key: str) -> tuple[int, str]:
    proc = subprocess.run(["git", "config", "--file", str(config), key], capture_output=True)
    return proc.returncode, proc.stdout.decode().strip()


def test_sibling_lane_writes_never_fail_or_remove(tmp_path):
    repo, wt = _setup_repo_and_worktree(tmp_path)
    path, baseline = init_guard(wt)
    sibling = {
        "remote.upstream.url": "https://example.invalid/up.git",
        "remote.upstream.fetch": "+refs/heads/*:refs/remotes/upstream/*",
        "remote.origin.pushurl": "ssh://example.invalid/o.git",
        "branch.wt-branch.description": "lane B state",
        "submodule.s.url": "https://example.invalid/s.git",
        "plangraph.lane.note": "lane B bookkeeping",
    }
    for key, value in sibling.items():
        _set(path, key, value)

    job = _job()
    check_guard((path, baseline), job)

    assert job.status == "completed"
    assert not any(e.get("event") == "git_config_mutated" for e in job.events)
    changed = [e for e in job.events if e.get("event") == "git_config_changed"]
    assert changed and changed[0]["keys"] == sorted(sibling)
    for key, value in sibling.items():
        assert _get(path, key) == (0, value)


def test_benign_key_change_is_not_mutation(tmp_path):
    # core.autocrlf is not a dangerous key: the change is recorded, but the
    # run is not failed and the value is never rewritten or removed.
    repo, wt = _setup_repo_and_worktree(tmp_path)
    path, baseline = init_guard(wt)
    _set(path, "core.autocrlf", "false")

    job = _job()
    check_guard((path, baseline), job)

    assert job.status == "completed"
    assert job.events == [{"event": "git_config_changed", "keys": ["core.autocrlf"]}]
    assert _get(path, "core.autocrlf") == (0, "false")


def test_dangerous_addition_fails_run_and_removes_only_offending_keys(tmp_path):
    repo, wt = _setup_repo_and_worktree(tmp_path)
    path, baseline = init_guard(wt)
    _set(path, "branch.x.remote", "origin")      # sibling lane state
    _set(path, "core.hooksPath", "secret-path")  # this run's writer

    job = _job()
    check_guard((path, baseline), job)

    assert job.status == "failed"
    assert job.exit_code == 1
    assert any(e == {"event": "git_config_mutated", "keys": ["core.hookspath"]} for e in job.events)
    assert "secret-path" not in str(job.events)
    assert _get(path, "core.hooksPath") == (1, "")
    assert _get(path, "branch.x.remote") == (0, "origin")


def test_changed_dangerous_key_restores_only_that_key(tmp_path):
    repo, wt = _setup_repo_and_worktree(tmp_path)
    config_file = repo / ".git" / "config"
    _set(config_file, "core.hooksPath", "good")  # pre-existing user value
    path, baseline = init_guard(wt)
    _set(path, "branch.x.remote", "origin")      # sibling lane state
    _set(path, "core.hooksPath", "evil")         # writer replaces the value

    job = _job()
    check_guard((path, baseline), job)

    assert job.status == "failed"
    assert _get(path, "core.hooksPath") == (0, "good")
    assert _get(path, "branch.x.remote") == (0, "origin")


def test_listing_error_warns_without_failing_or_restoring(tmp_path, monkeypatch):
    repo, wt = _setup_repo_and_worktree(tmp_path)
    path, baseline = init_guard(wt)
    _set(path, "branch.x.remote", "origin")      # sibling writes mid-run
    _set(path, "core.hooksPath", "secret")       # writer writes mid-run

    real_list = git_guard._list

    def flaky(target, *args, **kwargs):
        if Path(target).name == "config":
            raise OSError("transient")
        return real_list(target, *args, **kwargs)

    monkeypatch.setattr(git_guard, "_list", flaky)

    job = _job()
    check_guard((path, baseline), job)

    assert job.status == "completed"
    assert job.events == [{"event": "git_config_warning", "reason": "config_list_failed"}]
    # No restore on a listing error: nothing is removed or rewritten.
    assert _get(path, "branch.x.remote") == (0, "origin")
    assert _get(path, "core.hooksPath") == (0, "secret")


def test_unparseable_baseline_warns_without_failing(tmp_path, monkeypatch):
    repo, wt = _setup_repo_and_worktree(tmp_path)

    def flaky(target, *args, **kwargs):
        raise OSError("transient")

    monkeypatch.setattr(git_guard, "_list", flaky)
    guard = init_guard(wt)
    assert guard is not None and guard[1] is None
    monkeypatch.undo()

    path = guard[0]
    _set(path, "core.hooksPath", "secret")
    job = _job()
    check_guard(guard, job)

    assert job.status == "completed"
    assert job.events == [{"event": "git_config_warning", "reason": "baseline_unavailable"}]
    assert _get(path, "core.hooksPath") == (0, "secret")


def test_writer_identity_sets_committer_too():
    prompt, env = writer_launch("work", "implement", "agy/model", "run-123")
    assert prompt.endswith("Writer: agy/model run-123")
    assert env["GIT_COMMITTER_NAME"] == env["GIT_AUTHOR_NAME"] == "agy/model (plangraph writer)"
    assert env["GIT_COMMITTER_EMAIL"] == env["GIT_AUTHOR_EMAIL"] == "run-123@writer.plangraph.invalid"


def test_writer_dangerous_mutation_fails_and_removes_key(tmp_path):
    repo, wt = _setup_repo_and_worktree(tmp_path)
    config_file = repo / ".git" / "config"

    async def run():
        async def factory(*args, **kwargs):
            # Mutate core.hooksPath in worktree, which alters the shared <common-git-dir>/config
            subprocess.run(["git", "config", "core.hooksPath", "secret-path"], cwd=kwargs["cwd"],
                           check=True, capture_output=True)
            return FakeProcess(returncode=0)

        runner = SpawnerRunner(process_factory=factory)
        job = await runner.spawn(
            prompt="write code",
            model_or_combo="mimo/mimo-v2.6-flash",
            role="implement",
            cwd=str(wt),
            wait_seconds=0.5,
        )
        if job.job_id in runner._tasks:
            await runner._tasks[job.job_id]

        assert job.status == "failed"
        assert job.exit_code != 0
        # Verify no secrets in event
        for e in job.events:
            if e.get("event") == "git_config_mutated":
                assert e == {"event": "git_config_mutated", "keys": ["core.hookspath"]}
                assert "secret-path" not in str(e)

        # The offending key is removed; unrelated shared config is untouched.
        assert subprocess.run(["git", "config", "core.hooksPath"], cwd=repo,
                              capture_output=True).returncode == 1
        proc = subprocess.run(["git", "config", "core.autocrlf"], cwd=repo, capture_output=True, text=True, check=True)
        assert proc.stdout.strip() == "true"
        assert config_file.exists()

    asyncio.run(run())


def test_process_benign_config_change_completes(tmp_path):
    repo, wt = _setup_repo_and_worktree(tmp_path)

    async def run():
        async def factory(*args, **kwargs):
            subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=kwargs["cwd"],
                           check=True, capture_output=True)
            return FakeProcess(returncode=0)

        runner = SpawnerRunner(process_factory=factory)
        job = await runner.spawn(
            prompt="write code",
            model_or_combo="mimo/mimo-v2.6-flash",
            role="implement",
            cwd=str(wt),
            wait_seconds=0.5,
        )
        if job.job_id in runner._tasks:
            await runner._tasks[job.job_id]

        assert job.status == "completed"
        assert job.exit_code == 0
        event = next(e for e in job.events if e.get("event", "").startswith("git_config_"))
        assert event == {"event": "git_config_changed", "keys": ["core.autocrlf"]}
        # A non-dangerous key is never rewritten: sibling lanes may own it.
        proc = subprocess.run(["git", "config", "core.autocrlf"], cwd=repo, capture_output=True, text=True, check=True)
        assert proc.stdout.strip() == "false"

    asyncio.run(run())


@pytest.mark.parametrize("malicious", [False, True])
def test_sibling_branch_config_is_preserved(tmp_path, malicious):
    repo, wt = _setup_repo_and_worktree(tmp_path)

    async def run():
        async def factory(*args, **kwargs):
            for key, value in [("branch.x.remote", "origin"), ("branch.x.merge", "refs/heads/x")]:
                subprocess.run(["git", "config", key, value], cwd=repo, check=True)
            if malicious:
                subprocess.run(["git", "config", "core.hooksPath", "secret-path"], cwd=wt, check=True)
            return FakeProcess()

        runner = SpawnerRunner(process_factory=factory)
        job = await runner.spawn(prompt="write", model_or_combo="mimo/mimo-v2.6-flash",
                                 role="implement", cwd=str(wt), wait_seconds=.5)
        await runner._tasks[job.job_id]
        assert job.status == ("failed" if malicious else "completed")
        assert job.exit_code == (1 if malicious else 0)
        event = next(e for e in job.events if e["event"].startswith("git_config_"))
        assert event == {"event": "git_config_mutated" if malicious else "git_config_changed",
                         "keys": ["core.hookspath"] if malicious else ["branch.x.merge", "branch.x.remote"]}
        assert "secret-path" not in str(event)
        assert subprocess.check_output(["git", "config", "branch.x.remote"], cwd=repo).strip() == b"origin"
        assert subprocess.run(["git", "config", "core.hooksPath"], cwd=repo).returncode == 1

    asyncio.run(run())


def test_process_unchanged_config_ok(tmp_path):
    repo, wt = _setup_repo_and_worktree(tmp_path)
    config_file = repo / ".git" / "config"
    orig_bytes = config_file.read_bytes()

    async def run():
        async def factory(*args, **kwargs):
            return FakeProcess(returncode=0)

        runner = SpawnerRunner(process_factory=factory)
        job = await runner.spawn(
            prompt="write code",
            model_or_combo="mimo/mimo-v2.6-flash",
            role="implement",
            cwd=str(wt),
            wait_seconds=0.5,
        )
        if job.job_id in runner._tasks:
            await runner._tasks[job.job_id]

        assert job.status == "completed"
        assert job.exit_code == 0
        assert not any(e.get("event") == "git_config_mutated" for e in job.events)
        assert config_file.read_bytes() == orig_bytes

    asyncio.run(run())


def test_non_git_cwd_ok(tmp_path):
    non_git = tmp_path / "non_git_dir"
    non_git.mkdir()

    async def run():
        async def factory(*args, **kwargs):
            return FakeProcess(returncode=0)

        runner = SpawnerRunner(process_factory=factory)
        job = await runner.spawn(
            prompt="write code",
            model_or_combo="mimo/mimo-v2.6-flash",
            role="implement",
            cwd=str(non_git),
            wait_seconds=0.5,
        )
        if job.job_id in runner._tasks:
            await runner._tasks[job.job_id]

        assert job.status == "completed"
        assert job.exit_code == 0
        assert not any(e.get("event") == "git_config_mutated" for e in job.events)

    asyncio.run(run())


def test_fix_role_dangerous_mutation_fails_and_removes_key(tmp_path):
    repo, wt = _setup_repo_and_worktree(tmp_path)

    async def run():
        async def factory(*args, **kwargs):
            subprocess.run(["git", "config", "core.sshCommand", "evil"], cwd=kwargs["cwd"],
                           check=True, capture_output=True)
            return FakeProcess(returncode=0)

        runner = SpawnerRunner(process_factory=factory)
        job = await runner.spawn(
            prompt="fix bug",
            model_or_combo="mimo/mimo-v2.6-flash",
            role="fix",
            cwd=str(wt),
            wait_seconds=0.5,
        )
        if job.job_id in runner._tasks:
            await runner._tasks[job.job_id]

        assert job.status == "failed"
        assert job.exit_code != 0
        assert any(e.get("event") == "git_config_mutated" for e in job.events)
        assert subprocess.run(["git", "config", "core.sshCommand"], cwd=repo,
                              capture_output=True).returncode == 1

    asyncio.run(run())


def test_mutated_config_on_cancelled_job(tmp_path):
    repo, wt = _setup_repo_and_worktree(tmp_path)

    class BlockedMutatingProcess(FakeProcess):
        def __init__(self, cwd):
            super().__init__()
            self.cwd = cwd
            subprocess.run(["git", "config", "core.hooksPath", "secret-path"], cwd=self.cwd,
                           check=True, capture_output=True)

        async def communicate(self):
            await asyncio.Event().wait()

    async def killer(pid):
        pass

    async def run():
        async def factory(*args, **kwargs):
            return BlockedMutatingProcess(kwargs["cwd"])

        runner = SpawnerRunner(process_factory=factory, tree_killer=killer)
        job = await runner.spawn(
            prompt="write code",
            model_or_combo="mimo/mimo-v2.6-flash",
            role="implement",
            cwd=str(wt),
            wait_seconds=0.0,
        )
        await runner.cancel(job.job_id)
        await asyncio.gather(runner._tasks[job.job_id], return_exceptions=True)

        assert job.status == "failed"
        assert job.exit_code != 0
        assert any(e.get("event") == "git_config_mutated" for e in job.events)
        assert subprocess.run(["git", "config", "core.hooksPath"], cwd=repo,
                              capture_output=True).returncode == 1

    asyncio.run(run())
