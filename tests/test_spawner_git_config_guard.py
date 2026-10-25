import asyncio
import subprocess
from pathlib import Path

import pytest

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
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "core.autocrlf", "true"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "--allow-empty", "-m", "init"], cwd=repo, check=True, capture_output=True)

    wt = tmp_path / "worktree"
    subprocess.run(["git", "worktree", "add", str(wt), "-b", "wt-branch"], cwd=repo, check=True, capture_output=True)
    return repo, wt


def test_process_mutates_core_autocrlf_fails_and_restores(tmp_path):
    repo, wt = _setup_repo_and_worktree(tmp_path)
    config_file = repo / ".git" / "config"
    orig_bytes = config_file.read_bytes()

    async def run():
        async def factory(*args, **kwargs):
            # Mutate core.autocrlf in worktree, which alters the shared <common-git-dir>/config
            subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=kwargs["cwd"], check=True, capture_output=True)
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
        assert any(e.get("event") == "git_config_mutated" for e in job.events)
        # Verify no secrets in event
        for e in job.events:
            if e.get("event") == "git_config_mutated":
                assert list(e.keys()) == ["event"]
        assert config_file.read_bytes() == orig_bytes

        # Verify git config still sees restored value
        proc = subprocess.run(["git", "config", "core.autocrlf"], cwd=repo, capture_output=True, text=True, check=True)
        assert proc.stdout.strip() == "true"

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


def test_fix_role_mutating_config_fails_and_restores(tmp_path):
    repo, wt = _setup_repo_and_worktree(tmp_path)
    config_file = repo / ".git" / "config"
    orig_bytes = config_file.read_bytes()

    async def run():
        async def factory(*args, **kwargs):
            subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=kwargs["cwd"], check=True, capture_output=True)
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
        assert config_file.read_bytes() == orig_bytes

    asyncio.run(run())


def test_mutated_config_on_cancelled_job(tmp_path):
    repo, wt = _setup_repo_and_worktree(tmp_path)
    config_file = repo / ".git" / "config"
    orig_bytes = config_file.read_bytes()

    class BlockedMutatingProcess(FakeProcess):
        def __init__(self, cwd):
            super().__init__()
            self.cwd = cwd

        async def communicate(self):
            subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=self.cwd, check=True, capture_output=True)
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
        assert config_file.read_bytes() == orig_bytes

    asyncio.run(run())
