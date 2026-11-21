"""Hermetic stdin-delivery and Windows parity follow-up regressions."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from plangraph.spawner.runner import SpawnerRunner, _resolve_launch
from plangraph.routing_support.clients import build_standalone_fallback_command, launch_stdin_payload
from plangraph.routing_support.opencode_render import REFUSAL_TOO_LARGE, REFUSAL_UNRESOLVED, render_gateway_providers


@pytest.mark.parametrize('exe,size', [('agy.exe', 40000), ('agy.cmd', 9000), ('agy.bat', 9000)])
def test_agy_long_stream_payload(exe, size):
    prompt = 'é\n"' * size
    cmd = build_standalone_fallback_command('agy', prompt, 'test')
    payload = launch_stdin_payload(cmd, prompt, exe, is_nt=True)
    assert prompt not in cmd
    assert cmd[cmd.index('-p') + 1] == ''
    assert cmd[-4:] == ['--input-format', 'stream-json', '--output-format', 'stream-json']
    assert len(payload.splitlines()) == 1
    assert payload.endswith(b'\n')
    assert json.loads(payload) == {'event': 'user', 'message': {'role': 'user', 'content': prompt}}
    assert _resolve_launch(exe, cmd[1:], is_nt=True)[1] is None


@pytest.mark.parametrize('is_nt,prompt', [(True, 'short'), (False, 'x' * 40000)])
def test_agy_argv_unchanged(is_nt, prompt):
    cmd = build_standalone_fallback_command('agy', prompt, 'test')
    before = list(cmd)
    assert json.loads(launch_stdin_payload(cmd, prompt, 'agy.exe', is_nt=is_nt))['message']['content'] == prompt
    assert cmd == before == ['agy', '--dangerously-skip-permissions', '--model', 'test', '-p', '',
                            '--input-format', 'stream-json', '--output-format', 'stream-json']


@pytest.mark.parametrize('status,error', [('SUCCESS', ''), ('ERROR', 'recorded failure'), (None, 'agy_stream_missing_result')])
def test_runner_agy_stream_result(monkeypatch, status, error):
    async def run():
        events = [{'event': 'init'}, {'event': 'system'},
                  {'event': 'step_update', 'step_update': {'step_type': 'agent_response', 'text_delta': 'partial'}}]
        if status:
            events.extend([{'event': 'result', 'result': {'status': 'SUCCESS', 'response': 'old'}},
                           {'event': 'result', 'result': {'status': status, 'response': 'VERDICT: ACCEPT', 'error': error}}])
        proc = AsyncMock()
        proc.pid = None
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=('\n'.join(map(json.dumps, events)).encode(), b''))
        factory = AsyncMock(return_value=proc)
        monkeypatch.setattr('plangraph.spawner.runner._is_nt', lambda: True)
        monkeypatch.setattr('plangraph.spawner.target.resolve_target', lambda model: (model, None))
        monkeypatch.setattr('plangraph.planner.routing.validate_model', lambda model: None)
        monkeypatch.setattr('plangraph.spawner.resolver.host_skip_reason_async', AsyncMock(return_value=None))
        prompt = 'x' * 40000
        job = await SpawnerRunner(process_factory=factory).spawn(
            prompt, 'gemini-4-neon-medium', client='agy', role='review', wait_seconds=2)
        assert job.status == ('completed' if status == 'SUCCESS' else 'failed')
        assert job.stdout == ('VERDICT: ACCEPT' if status == 'SUCCESS' else '')
        assert error in job.stderr
        assert (job.exit_code == 0) == (status == 'SUCCESS')
        assert prompt not in factory.call_args.args
        assert factory.call_args.kwargs['stdin'] == asyncio.subprocess.PIPE
        payload = proc.communicate.call_args.kwargs['input']
        assert json.loads(payload)['message']['content'].endswith(prompt)
    asyncio.run(run())


@pytest.mark.parametrize('exe', ['qodercli.exe', 'qodercli.cmd'])
def test_long_qoder_stdin(exe):
    prompt = 'é\n"&' * 12000
    cmd = build_standalone_fallback_command('qodercli', prompt, 'qoder/test')
    payload = launch_stdin_payload(cmd, prompt, exe, is_nt=True)
    assert payload == prompt.encode('utf-8')
    assert prompt not in cmd
    assert cmd[3:5] == ['--input-format', 'text']
    assert cmd[cmd.index('-p') + 1] == ''
    assert cmd == ['qodercli', '-p', '', '--input-format', 'text', '--model', 'qoder/test']
    assert _resolve_launch(exe, cmd[1:], is_nt=True)[1] is None


def test_size_refusal_does_not_mutate_env():
    import json
    env = {'OPENCODE_CONFIG_CONTENT': json.dumps({'padding': 'x' * 31000,
           'providers': {'mimo': {'settings': {'baseURL': 'https://gateway.invalid/v1'}}}})}
    before = dict(env)
    assert render_gateway_providers(env, 'mimo/mimo-v2.6-flash', is_nt=True) == REFUSAL_TOO_LARGE
    assert env == before


def test_runner_long_qoder_uses_existing_stdin_path(monkeypatch):
    async def run():
        prompt = 'x' * 40000
        proc = AsyncMock()
        proc.pid = None
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b'VERDICT: ACCEPT', b''))
        factory = AsyncMock(return_value=proc)
        monkeypatch.setattr('plangraph.spawner.runner._is_nt', lambda: True)
        monkeypatch.setattr('plangraph.spawner.target.resolve_target', lambda model: (model, None))
        monkeypatch.setattr('plangraph.planner.routing.validate_model', lambda model: None)
        monkeypatch.setattr('plangraph.spawner.resolver.host_skip_reason_async', AsyncMock(return_value=None))
        job = await SpawnerRunner(process_factory=factory).spawn(
            prompt, 'Qwen3.8-Flash', client='qodercli', role='review', wait_seconds=2)
        assert job.status == 'completed'
        assert prompt not in factory.call_args.args
        assert factory.call_args.kwargs['stdin'] == asyncio.subprocess.PIPE
        assert proc.communicate.await_args.kwargs['input'].endswith(prompt.encode())
        assert job.stdout == 'VERDICT: ACCEPT'
    asyncio.run(run())


def test_rejected_stdin_never_retries_empty(monkeypatch):
    class RejectingProcess:
        returncode = 0
        empty_calls = 0

        async def communicate(self, **kwargs):
            if 'input' in kwargs:
                raise TypeError('unsupported input')
            self.empty_calls += 1
            return b'empty prompt completed', b''

    async def run():
        proc = RejectingProcess()
        monkeypatch.setattr('plangraph.spawner.resolver.host_skip_reason_async', AsyncMock(return_value=None))
        runner = SpawnerRunner(process_factory=AsyncMock(return_value=proc))
        job = await runner.spawn('test', 'claude/claude-haiku-4-5', client='claude', role='review', wait_seconds=2)
        assert job.status == 'failed'
        assert job.exit_code != 0
        assert 'stdin_delivery_failed' in job.stderr
        assert proc.empty_calls == 0
    asyncio.run(run())


@pytest.mark.parametrize('harness', ['agy', 'qodercli'])
@pytest.mark.parametrize('tail', [[], ['-p'], ['-p', '--model']])
def test_long_prompt_missing_flag_fails_named(harness, tail):
    with pytest.raises(RuntimeError, match='stdin_prompt_flag_missing'):
        launch_stdin_payload([harness, 'x' * 40000, *tail], 'prompt', harness + '.exe', is_nt=True)


@pytest.mark.parametrize('closed', [False, True])
def test_unavailable_stdin_records_failure(closed):
    from types import SimpleNamespace
    from plangraph.spawner.runner import JobInfo
    from plangraph.spawner.run_watchdog import collect

    async def run():
        class ClosedPipe:
            def write(self, payload):
                raise BrokenPipeError()

            def close(self):
                pass

        job = JobInfo('test', 'running', '', 'fixture/model', role='review')
        runner = SimpleNamespace(jobs={'test': job}, _child_envs={}, _stdin_payloads={'test': b'prompt'})
        proc = SimpleNamespace(stdin=ClosedPipe() if closed else None,
                               stdout=asyncio.StreamReader(), stderr=asyncio.StreamReader(),
                               returncode=7)
        proc.stderr.feed_data(b'Error: HTTP 429 rate limited\n')
        proc.stderr.feed_eof()
        proc.stdout.feed_eof()
        stdout, stderr = await collect(runner, 'test', proc)
        assert stdout == b''
        assert stderr == b'Error: HTTP 429 rate limited\n'
        assert (job.status, job.exit_code) == ('failed', 7)
        assert '429' in job.refusal
        assert job.events == [{'event': 'stdin_delivery_failed'}]
    asyncio.run(run())


def test_runner_without_gateway_url_refuses(monkeypatch):
    for name in ('PLANGRAPH_GATEWAY_URL', 'OMNIROUTE_BASE_URL', 'AUTOOS_OMNIROUTE_URL', 'OPENCODE_CONFIG_CONTENT'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr('plangraph.spawner.opencode_overlay.overlay_env', lambda env: env)

    async def run():
        factory = AsyncMock()
        job = await SpawnerRunner(process_factory=factory).spawn('test', 'mimo/mimo-v2.6-flash', role='review')
        assert job.status == 'failed'
        assert job.stderr == REFUSAL_UNRESOLVED
        factory.assert_not_called()
    asyncio.run(run())


def test_async_admission_preserves_render_refusal(monkeypatch):
    from plangraph.routing_support import resolver, opencode_render
    probe = AsyncMock()
    monkeypatch.setattr(resolver, 'host_probe_env', lambda env=None: {})
    monkeypatch.setattr(opencode_render, 'render_gateway_providers', lambda env, model: REFUSAL_TOO_LARGE)
    monkeypatch.setattr(resolver, 'served_models_async', probe)
    reason = asyncio.run(resolver.host_skip_reason_async(
        {'model': 'mimo/mimo-v2.6-flash', 'harness': 'opencode'}))
    assert reason == REFUSAL_TOO_LARGE
    probe.assert_not_awaited()


@pytest.fixture
def hydrogen_review(tmp_path, monkeypatch):
    from pathlib import Path

    from plangraph.routing_support.leg_state import LegStateStore
    from plangraph.spawner.service import SpawnService

    original = tmp_path / "operator"
    mcp = original / ".gemini/config/mcp_config.json"
    mcp.parent.mkdir(parents=True)
    mcp.write_text('{"mcpServers":{"stalling":{"command":"false"}}}')
    login = Path(".gemini") / "antigravity-cli" / "antigravity-oauth-token"
    (original / login).parent.mkdir()
    (original / login).write_text("fixture account material")
    monkeypatch.setenv("HOME", str(original))
    monkeypatch.setenv("USERPROFILE", str(original))
    monkeypatch.setattr('plangraph.spawner.resolver.host_skip_reason_async', AsyncMock(return_value=None))
    monkeypatch.setattr('plangraph.spawner.service.admit_base', AsyncMock(return_value=True))

    proc = AsyncMock(pid=None, returncode=0)
    launches = []

    async def launch(*args, env, cwd, **kwargs):
        launches.append((args, env, cwd, kwargs))
        home = Path(env["HOME"])
        assert home != original
        assert not (home / mcp.relative_to(original)).exists()
        assert (home / login).read_text() == "fixture account material"
        assert not (home / login).is_symlink()
        return proc

    runner = SpawnerRunner(default_cwd=str(tmp_path), process_factory=launch)
    service = SpawnService(runner, state=tmp_path / "state", leg_reader=LegStateStore(tmp_path / "legs.sqlite"))
    monkeypatch.setattr(service, "admit_spawn", lambda *args, **kwargs: None)
    yield service, proc, launches
    assert "stalling" in mcp.read_text()


def assert_hydrogen_wire(proc, launches, prompt, probe=False):
    from pathlib import Path

    from plangraph.spawner.prompt_input import compose

    ((exe, *argv), env, cwd, kwargs), = launches
    assert Path(exe).name.removesuffix(".exe") == "agy"
    mode, flag, settings, *transport = argv[1:]
    assert (argv[0], mode, flag) == ("--mode", "plan", "--settings")
    assert json.loads(settings) == {"permissions": {"allow": [
        "command(*)", "read_file(*)", "list_dir(*)", "grep_search(*)", "find_by_name(*)"]}}
    assert transport == ["--model", "gemini-4-hydrogen-high", "-p", "",
                         "--input-format", "stream-json", "--output-format", "stream-json"]
    assert kwargs["stdin"] == asyncio.subprocess.PIPE
    assert not Path(env["HOME"]).exists()
    wire = proc.communicate.call_args.kwargs["input"]
    content = compose(prompt, "review", "agy", cwd, "agy-gemini-4-hydrogen-high")
    envelope = {"event": "user", "message": {"role": "user", "content": content}}
    assert wire == (json.dumps(envelope, ensure_ascii=False) + "\n").encode()
    sentence = "Answer in plain text in this reply only; do not create a plan file"
    assert sentence + "." in content.splitlines()[0]
    if probe:
        assert sentence + " or any other file." in content


def test_hydrogen_review_completes_with_the_result_reply(hydrogen_review):
    service, proc, _ = hydrogen_review
    proc.communicate = AsyncMock(return_value=(
        b'{"event":"result","result":{"status":"SUCCESS","response":"OK"}}\n', b''))
    record = asyncio.run(service.spawn(
        card={"estimate_tokens": 1000}, prompt="Reply OK", role="review", writer_families=("gpt",),
        leg="agy-gemini-4-hydrogen-high", override_reason="D-6550 Hydrogen review fixture",
        no_fallthrough=True, wait_seconds=None))
    assert (record["status"], record["stdout_tail"]) == ("completed", "OK")


def test_hydrogen_artifact_without_result_records_no_verdict(hydrogen_review):
    service, proc, launches = hydrogen_review
    proc.communicate = AsyncMock(return_value=(
        b'{"event":"init"}\n'
        b'{"event":"step_update","step_update":{"step_type":"agent_response",'
        b'"text_delta":"I have created implementation_plan.md. Please review and approve it."}}\n', b''))
    prompt = "Review the proposed change."
    record = asyncio.run(service.spawn(
        card={"estimate_tokens": 1000}, prompt=prompt, role="review", writer_families=("gpt",),
        leg="agy-gemini-4-hydrogen-high", override_reason="D-6550 Hydrogen review fixture",
        no_fallthrough=True, wait_seconds=None))
    assert record["status"] == "failed" and record["exit_code"] == 1
    assert record["reason"] == "no_verdict"
    assert record["review_reason"] == "agy_stream_missing_result"
    assert record["stdout_tail"] == ""
    assert "agy_stream_missing_result" in record["stderr_tail"]
    assert_hydrogen_wire(proc, launches, prompt)


@pytest.mark.parametrize("response,exit_code,reason", [
    ("OK", 0, ""), (None, 7, "exit 7: agy_stream_missing_result"),
    ("I have created implementation_plan.md.", 0, "unexpected review answer"),
])
def test_named_hydrogen_probe_runs_review_transport(hydrogen_review, monkeypatch, capsys,
                                                   response, exit_code, reason):
    from plangraph.spawner import cli, leg_probe

    service, proc, launches = hydrogen_review
    proc.returncode = exit_code
    stream = b'{"event":"init"}\n'
    if response is not None:
        stream += json.dumps({"event": "result", "result": {
            "status": "SUCCESS", "response": response}}).encode() + b"\n"
    proc.communicate = AsyncMock(return_value=(stream, b''))
    monkeypatch.setattr(cli, "SpawnService", lambda **kwargs: service)
    monkeypatch.setattr(leg_probe.shutil, "which", lambda *args, **kwargs: "agy")
    result = cli.main(["--state", str(service.state), "probe", "--leg",
                       "agy-gemini-4-hydrogen-high", "--json", "--timeout", "5"])
    row, = json.loads(capsys.readouterr().out)["probes"]
    assert result == (1 if reason else 0)
    assert row["ok"] == (not reason) and not row["infra"]
    assert row["reason"] == reason
    assert_hydrogen_wire(proc, launches, leg_probe.REVIEW_PROMPT, probe=True)
