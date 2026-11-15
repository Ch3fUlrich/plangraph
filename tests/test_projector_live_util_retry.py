"""Hermetic startup regression tests: no sockets, subprocesses or Temporal binary."""
from types import SimpleNamespace

import pytest
from temporalio.service import RPCError, RPCStatusCode

import projector_live_util as util


@pytest.fixture
def fake(monkeypatch, tmp_path):
    state = SimpleNamespace(processes=[], commands=[], addresses=[], sleeps=[], checks=[],
                            exits=[], clock=0.0)
    ports = iter(range(21000, 21100, 4))
    monkeypatch.setattr(util, "_free_ports", lambda n: list(range(p := next(ports), p + n)))

    def popen(cmd, **kwargs):
        index = len(state.processes)
        kwargs["stdout"].write(("x" * 2000 + f"cause-{index + 1}").encode())
        proc = SimpleNamespace(pid=90000 + index, waits=0)
        proc.poll = lambda: state.exits[index] if index < len(state.exits) else None

        def wait(**kwargs):
            proc.waits += 1
        proc.wait = wait
        state.processes.append(proc)
        state.commands.append(cmd)
        return proc

    def source(*, address):
        state.addresses.append(address)

        def check():
            value = state.checks.pop(0) if state.checks else True
            if isinstance(value, Exception):
                raise value
            return value
        return SimpleNamespace(connect_check=check)

    def sleep(seconds):
        state.sleeps.append(seconds)
        state.clock += seconds
    monkeypatch.setattr(util.subprocess, "Popen", popen)
    monkeypatch.setattr(util.os, "killpg", lambda *args: None)
    monkeypatch.setattr(util, "TemporalSource", source)
    monkeypatch.setattr(util.DevServer, "_namespace_ready", staticmethod(lambda source: True))
    monkeypatch.setattr(util.time, "monotonic", lambda: state.clock)
    monkeypatch.setattr(util.time, "sleep", sleep)
    state.server = util.DevServer("fake", tmp_path, http_api=True)
    yield state
    state.server.stop()


def test_first_exit_then_ready(fake):
    fake.exits = [1, None]
    fake.server.start()
    assert len(fake.processes) == 2
    assert [p.waits for p in fake.processes] == [1, 0]
    assert fake.addresses == ["127.0.0.1:21000", "127.0.0.1:21004"]
    assert fake.server.address == fake.addresses[-1]
    assert (fake.server.port, fake.server.http_port) == (21004, 21007)
    for flag in ("--port", "--ui-port", "--metrics-port", "--http-port", "--db-filename"):
        assert fake.commands[0][fake.commands[0].index(flag) + 1] != fake.commands[1][
            fake.commands[1].index(flag) + 1]
    assert fake.sleeps == []


@pytest.mark.parametrize("exited", [True, False])
def test_all_attempts_fail(fake, exited):
    fake.exits = [1] * 3 if exited else []
    fake.checks = [False] * 20
    with pytest.raises(RuntimeError, match="did not become ready after 3 attempts") as error:
        fake.server.start(timeout=0.5)
    assert len(fake.processes) == 3
    assert all(p.waits == 1 for p in fake.processes)
    assert fake.server.proc is None
    for n in range(1, 4):
        assert f"attempt {n}:" in str(error.value)
        assert f"cause-{n}" in str(error.value)
    assert len(str(error.value)) < 4700


def test_readiness_rpc_retried(fake):
    fake.checks = [RPCError("tcp connect error", RPCStatusCode.UNAVAILABLE, b""), True]
    fake.server.start()
    assert len(fake.processes) == 1
    assert fake.sleeps == [0.3]


def test_non_rpc_immediate(fake):
    fake.checks = [ValueError("bad configuration")]
    with pytest.raises(ValueError, match="bad configuration"):
        fake.server.start()
    assert len(fake.processes) == 1
    assert fake.processes[0].waits == 1
    assert fake.sleeps == []


def test_first_ready_no_delay(fake):
    fake.server.start()
    assert len(fake.processes) == 1
    assert fake.sleeps == []
    assert fake.server.address == fake.addresses[0]



def rig_with_start_results(monkeypatch, fake, results, *, clock_jump=0.3):
    import asyncio
    calls = []

    async def start(*args, **kwargs):
        calls.append((args, kwargs))
        assert len(calls) <= 100, "unbounded namespace readiness retry"
        value = results.pop(0) if len(results) > 1 else results[0]
        if isinstance(value, Exception):
            raise value
        return value

    async def sleep(seconds):
        fake.sleeps.append(seconds)
        fake.clock += clock_jump

    rig = object.__new__(util.LiveRig)
    rig.client = SimpleNamespace(start_workflow=start)
    rig.gate_queue = "live-gate"
    rig.run = asyncio.run
    monkeypatch.setattr(util.asyncio, "sleep", sleep)
    return rig, calls


def test_gate_start_retries_namespace_cache_race(monkeypatch, fake):
    unavailable = RPCError("Namespace default is not found.", RPCStatusCode.NOT_FOUND, b"")
    rig, calls = rig_with_start_results(monkeypatch, fake,
        [unavailable, SimpleNamespace(result_run_id="R2")])
    assert rig.start_gate("card:A", "demo:A") == "R2"
    assert len(calls) == 2 and fake.sleeps == [0.3]
    assert all(kwargs["memo"] == {"plan_key": "demo:A"} for _, kwargs in calls)


@pytest.mark.parametrize("status,message", [
    (RPCStatusCode.NOT_FOUND, "workflow missing"),
    (RPCStatusCode.PERMISSION_DENIED, "Namespace default is not found."),
])
def test_gate_start_preserves_non_readiness_errors(monkeypatch, fake, status, message):
    problem = RPCError(message, status, b"")
    rig, calls = rig_with_start_results(monkeypatch, fake, [problem])
    with pytest.raises(RPCError) as exc:
        rig.start_gate("card:A", "demo:A")
    assert exc.value is problem
    assert len(calls) == 1 and fake.sleeps == []


def test_gate_start_namespace_retry_has_a_deadline(monkeypatch, fake):
    problem = RPCError("Namespace default is not found.", RPCStatusCode.NOT_FOUND, b"")
    rig, calls = rig_with_start_results(monkeypatch, fake, [problem], clock_jump=100)
    with pytest.raises(RPCError) as exc:
        rig.start_gate("card:A", "demo:A")
    assert exc.value is problem
    assert len(calls) <= 2 and fake.sleeps == [0.3]
