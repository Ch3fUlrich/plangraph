"""Tests for Windows prompt delivery via stdin and launch guards (plangraph:SPAWN-WIN-PARITY, S2).

Invariants I9-I12, I16:
- I9: prompt delivered as UTF-8 on stdin, never in argv; dry-run output shows [stdin: N bytes].
- I10: no prompt file written to disk.
- I11: on nt, .cmd/.bat opencode swapped for sibling exe when it exists; never shell=True.
- I12: on nt, non-opencode argv length checked against CreateProcess limits (32000 / 8000).
- I16: hermetic, no network, no real opencode, no real keys.
- D-2851: module-level _is_nt() behaviour test.
"""
from __future__ import annotations

import asyncio
import os
import stat
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import plangraph.spawner.runner as runner_module
from plangraph.spawner.runner import IMPLEMENT_HEADER, SpawnerRunner


def _sent(prompt):
    """The prompt an implement-role spawn sends (D-2966 header first)."""
    return IMPLEMENT_HEADER + "\n\n" + prompt


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.setenv("OMNIROUTE_BASE_URL", "http://127.0.0.1:20128")
    monkeypatch.delenv("PLANGRAPH_SPAWN_RUN_TIMEOUT", raising=False)


def test_opencode_prompt_via_stdin_and_not_in_argv(monkeypatch):
    """(a) 70 000-char prompt with newlines, non-ASCII and %^&|\"<>:

    - argv contains no prompt substring
    - stdin=PIPE passed to process_factory
    - communicate(input=...) receives prompt.encode('utf-8')
    """
    async def run():
        special = "hello %^&|\"<> \n\t café 🚀 \n"
        prompt = special + ("A" * (70000 - len(special)))
        prompt_bytes = _sent(prompt).encode("utf-8")

        proc = AsyncMock()
        proc.returncode = 0
        proc.pid = 9999
        proc.communicate = AsyncMock(return_value=(b"done", b""))

        factory_calls = []

        async def fake_factory(*args, **kwargs):
            factory_calls.append((args, kwargs))
            return proc

        monkeypatch.setattr(
            "plangraph.spawner.resolver.host_skip_reason_async",
            AsyncMock(return_value=None),
        )

        runner = SpawnerRunner(process_factory=fake_factory)
        job = await runner.spawn(
            prompt=prompt,
            model_or_combo="mimo/mimo-v2.6-flash",
            wait_seconds=1.0,
        )

        assert len(factory_calls) == 1
        call_args, call_kwargs = factory_calls[0]

        # Invariant I9: prompt is never in argv
        for arg in call_args:
            assert prompt not in arg
            assert "%^&|\"<>" not in arg
        for arg in job.command:
            assert prompt not in arg

        # Stdin is PIPE
        assert call_kwargs.get("stdin") == asyncio.subprocess.PIPE

        # Communicate input is the prompt bytes
        proc.communicate.assert_awaited()
        first_call = proc.communicate.call_args_list[0]
        assert first_call.kwargs.get("input") == prompt_bytes

    asyncio.run(run())


def test_hermetic_real_process_stdin_count(tmp_path, monkeypatch):
    """(b) Hermetic real-process test: tmp opencode script on PATH writes len(stdin) to stdout."""
    async def run():
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        script = bin_dir / "opencode"
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "if len(sys.argv) > 1 and sys.argv[1] == 'models':\n"
            "    sys.stdout.write('mimo/mimo-v2.6-flash\\n')\n"
            "    sys.exit(0)\n"
            "data = sys.stdin.buffer.read()\n"
            "sys.stdout.write(str(len(data)))\n"
        )
        script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
        monkeypatch.setattr(
            "plangraph.spawner.resolver.host_skip_reason_async",
            AsyncMock(return_value=None),
        )

        prompt = "X" * 70000
        runner = SpawnerRunner()
        job = await runner.spawn(
            prompt=prompt,
            model_or_combo="mimo/mimo-v2.6-flash",
            wait_seconds=5.0,
        )

        assert job.status == "completed", (job.exit_code, job.events, job.stderr[-800:])
        assert job.exit_code == 0
        assert job.stdout == str(len(_sent(prompt).encode("utf-8")))
        for arg in job.command:
            assert prompt not in arg

    asyncio.run(run())


def test_resolve_launch_windows_guard(tmp_path, monkeypatch):
    """(c) _resolve_launch with is_nt=True:

    - tmp opencode.cmd + sibling exe -> returns sibling exe (I11)
    - with no exe -> .cmd stays
    - agy argv > 32000 chars -> refusal, factory never called (I12)
    """
    _resolve_launch = getattr(runner_module, "_resolve_launch", None)
    assert callable(_resolve_launch), "_resolve_launch helper must exist"

    # 1. opencode.cmd with sibling exe
    dir1 = tmp_path / "shim1"
    dir1.mkdir()
    cmd1 = dir1 / "opencode.cmd"
    cmd1.write_text("@echo off\n")
    exe1 = dir1 / "node_modules" / "@opencode" / "cli" / "bin" / "opencode.exe"
    exe1.parent.mkdir(parents=True)
    exe1.write_text("binary")

    res1, err1 = _resolve_launch(str(cmd1), ["run"], is_nt=True)
    assert res1 == str(exe1)
    assert err1 is None

    # Case-insensitive .BAT check
    bat1 = dir1 / "opencode.BAT"
    bat1.write_text("@echo off\n")
    res1_bat, err1_bat = _resolve_launch(str(bat1), ["run"], is_nt=True)
    assert res1_bat == str(exe1)
    assert err1_bat is None

    # 2. opencode.cmd without sibling exe stays
    dir2 = tmp_path / "shim2"
    dir2.mkdir()
    cmd2 = dir2 / "opencode.cmd"
    cmd2.write_text("@echo off\n")

    res2, err2 = _resolve_launch(str(cmd2), ["run"], is_nt=True)
    assert res2 == str(cmd2)
    assert err2 is None

    # 3. Direct _resolve_launch refusal on long argv for non-opencode
    res3, err3 = _resolve_launch("agy", ["-p", "x" * 40000], is_nt=True)
    assert err3 is not None
    assert err3.startswith("command_line_too_long:")
    assert "chars" in err3

    # .cmd non-opencode refusal at >8000 limit
    cmd_agy = dir2 / "agy.cmd"
    cmd_agy.write_text("@echo off\n")
    res_bat_agy, err_bat_agy = _resolve_launch(str(cmd_agy), ["-p", "x" * 8500], is_nt=True)
    assert err_bat_agy is not None
    assert err_bat_agy.startswith("command_line_too_long:")

    # 4. Spawner runner integration: agy argv of 40 000 chars refuses and factory is never called
    async def run_spawn():
        factory = AsyncMock()
        runner = SpawnerRunner(process_factory=factory)
        monkeypatch.setattr(runner_module, "_is_nt", lambda: True)
        monkeypatch.setattr(
            "plangraph.spawner.resolver.host_skip_reason_async",
            AsyncMock(return_value=None),
        )

        job = await runner.spawn(
            prompt="x" * 40000,
            model_or_combo="agy/claude-sonnet-5-5-high",
            client="agy",
        )
        assert job.status == "failed"
        assert job.exit_code == 1
        assert "command_line_too_long" in job.stderr
        factory.assert_not_called()

    asyncio.run(run_spawn())


def test_timeout_path_with_stdin_kills_and_records_124(monkeypatch):
    """(d) The timeout path (PLANGRAPH_SPAWN_RUN_TIMEOUT=0.2) with stdin still kills and records 124."""
    async def run():
        monkeypatch.setenv("PLANGRAPH_SPAWN_RUN_TIMEOUT", "0.2")
        monkeypatch.setattr(
            "plangraph.spawner.resolver.host_skip_reason_async",
            AsyncMock(return_value=None),
        )

        proc = AsyncMock()
        proc.pid = 4321
        proc.returncode = 0

        communicate_calls = []

        async def fake_communicate(input=None):
            communicate_calls.append(input)
            if len(communicate_calls) == 1:
                await asyncio.sleep(5.0)
                return b"timed out stdout", b""
            return b"post kill stdout", b""

        proc.communicate = fake_communicate

        killer = AsyncMock()

        runner = SpawnerRunner(
            process_factory=AsyncMock(return_value=proc),
            tree_killer=killer,
        )

        prompt = "some test prompt"
        job = await runner.spawn(
            prompt=prompt,
            model_or_combo="mimo/mimo-v2.6-flash",
            wait_seconds=1.0,
        )

        killer.assert_awaited_once_with(proc.pid)

        assert len(communicate_calls) == 2
        assert communicate_calls[0] == _sent(prompt).encode("utf-8")
        assert communicate_calls[1] is None

        assert job.status == "timeout"  # Watchdog exposes a distinct terminal status.
        assert job.exit_code == 124
        assert any(e.get("event") == "run_timeout" for e in job.events)

    asyncio.run(run())


def test_dry_run_stdout_no_prompt_text():
    """(e) Dry-run stdout has no prompt text and includes [stdin: N bytes]."""
    async def run():
        runner = SpawnerRunner()
        prompt = "SECRET_PROMPT_TEXT_ABC123"
        prompt_len = len(_sent(prompt).encode("utf-8"))

        job = await runner.spawn(
            prompt=prompt,
            model_or_combo="mimo/mimo-v2.6-flash",
            dry_run=True,
        )

        assert prompt not in job.stdout
        assert f"[stdin: {prompt_len} bytes]" in job.stdout
        for arg in job.command:
            assert prompt not in arg
        assert job.task_prompt == _sent(prompt)

    asyncio.run(run())


def test_is_nt_hook_behaviour(monkeypatch):
    """D-2851: module-level _is_nt() behaviour test."""
    _is_nt = getattr(runner_module, "_is_nt", None)
    assert callable(_is_nt), "_is_nt helper must exist"
    assert _is_nt() == (os.name == "nt")

    _resolve_launch = getattr(runner_module, "_resolve_launch", None)
    assert callable(_resolve_launch)

    monkeypatch.setattr(runner_module, "_is_nt", lambda: False)
    exe, reason = _resolve_launch("agy", ["-p", "x" * 40000])
    assert reason is None

    monkeypatch.setattr(runner_module, "_is_nt", lambda: True)
    exe, reason = _resolve_launch("agy", ["-p", "x" * 40000])
    assert reason is not None
    assert reason.startswith("command_line_too_long:")
