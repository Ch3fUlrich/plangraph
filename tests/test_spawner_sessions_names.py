"""tests/test_spawner_sessions_names.py - D-3605: <role>-NNNN session names."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import pytest
from plangraph.routing_errors import ParameterRefused
from sessions_support import UUID1, UUID2, PROMPT, FakeRun, make, go, write_config, site  # noqa: F401


def _mode(site, mode):
    write_config(site.cfg, json.dumps({**json.loads(site.cfg.read_text()), "session_names": mode}))
    return site


def _handoff(site):
    hf = site.tmp_path / "handoff.md"
    hf.write_text("carry on")
    return hf


@pytest.fixture
def rc(site):  # noqa: F811
    return _mode(site, "role-counter")


def _names(site):
    return sorted(p.stem for p in (site.state / "sessions").glob("*-????.json"))


def test_counter_starts_at_0001_and_increments(rc):
    s = make(rc, FakeRun(stdout="session " + UUID1))
    assert go(rc, s, "alpha")["name"] == "alpha-0001"
    assert go(rc, s, "alpha")["name"] == "alpha-0002"
    assert go(rc, s, "beta")["name"] == "beta-0001"
    assert _names(rc) == ["alpha-0001", "alpha-0002", "beta-0001"]
    assert json.loads((rc.state / "sessions" / "_counters.json").read_text()) == {"alpha": 2, "beta": 1}
    (rc.state / "sessions" / "counters.json").write_text('{"alpha": 9}')  # pre-rename data file: no phantom row
    assert [x["name"] for x in s.list()["sessions"]] == ["alpha-0001", "alpha-0002", "beta-0001"]


def test_number_never_reused_after_stop_or_record_loss(rc):
    s = make(rc, FakeRun(stdout="session " + UUID1))
    go(rc, s, "alpha")
    s.stop(name="alpha-0001")
    assert go(rc, s, "alpha")["name"] == "alpha-0002"
    (rc.state / "sessions" / "alpha-0002.json").unlink()  # the counter file still remembers it
    assert go(rc, s, "alpha")["name"] == "alpha-0003"
    (rc.state / "sessions" / "_counters.json").unlink()  # the records still remember it
    assert go(rc, s, "alpha")["name"] == "alpha-0004"


def test_explicit_names(rc):
    s = make(rc, FakeRun(stdout="session " + UUID1))
    assert go(rc, s, "alpha-0001")["name"] == "alpha-0001"  # exactly the next number
    for wrong in ("alpha-0007", "alpha-0001"):
        with pytest.raises(ParameterRefused, match="expected alpha-0002"):
            go(rc, s, wrong)
    for bad in ("alpha-001", "alpha-00002", "alpha-12", "alpha-0"):
        with pytest.raises(ParameterRefused, match="exactly 4 digits"):
            go(rc, s, bad)
    assert _names(rc) == ["alpha-0001"]


def test_dry_run_shows_name_and_does_not_bump(rc):
    run = FakeRun(stdout="session " + UUID1)
    s = make(rc, run)
    for _ in range(2):  # argv shows the name the launch would use (after -n)
        argv = go(rc, s, "alpha", dry_run=True)["argv"]
        assert argv[argv.index("-n") + 1] == "alpha-0001"
    assert not (rc.state / "sessions" / "_counters.json").exists() and not run.launch_calls()
    assert go(rc, s, "alpha")["name"] == "alpha-0001"


def test_parallel_launches_get_distinct_numbers(rc):
    s = make(rc, FakeRun(stdout="session " + UUID1))
    with ThreadPoolExecutor(8) as pool:
        got = [f.result()["name"] for f in [pool.submit(go, rc, s, "gamma") for _ in range(8)]]
    assert sorted(got) == [f"gamma-{n:04d}" for n in range(1, 9)]


def test_relaunch_starts_a_new_numbered_session(rc):
    run = FakeRun(stdout="session " + UUID1)
    s = make(rc, run)
    go(rc, s, "alpha")
    sessions = rc.state / "sessions"
    hf = _handoff(rc)
    s.mark_ready("alpha-0001", str(hf))
    run.stdout = "session " + UUID2
    res = s.relaunch("alpha-0001")
    assert (res["name"], res["previous_name"], res["session_id"]) == ("alpha-0002", "alpha-0001", UUID2)
    old, new = (json.loads((sessions / f"alpha-000{n}.json").read_text()) for n in (1, 2))
    assert (old["state"], old["session_id"], new["state"], new["session_id"]) == ("stopped", UUID1, "running", UUID2)
    assert new["handoff"] == str(hf.resolve()) and new["prompt_file"] == str(sessions / "alpha-0002.prompt.txt")
    assert [(sessions / f"alpha-000{n}.prompt.txt").read_text() for n in (1, 2)] == [PROMPT, PROMPT]
    assert not (sessions / "alpha-0001.ready.json").exists()
    assert run.launch_calls()[1][-1] == f"Restart from handoff {hf.resolve()}.\n\n" + PROMPT
    assert all('input' not in kw for _, kw in run.calls)
    assert go(rc, s, "alpha")["name"] == "alpha-0003"  # relaunch took 0002
    s.stop(name="alpha-0003")  # a later live record of the role would supersede 0002
    assert s.relaunch("alpha-0002", handoff=str(hf))["name"] == "alpha-0004"


def test_relaunch_of_a_legacy_name_gets_a_numbered_session(rc):
    s = make(rc, FakeRun(stdout="session " + UUID1))
    go(_mode(rc, "free"), s, "alpha")
    hf = _handoff(_mode(rc, "role-counter"))
    go(rc, s, "alpha")  # alpha-0001 is live, yet it does not supersede the digit-less legacy record
    res = s.relaunch("alpha", handoff=str(hf))
    assert (res["name"], res["previous_name"]) == ("alpha-0002", "alpha")
    assert json.loads((rc.state / "sessions" / "alpha.json").read_text())["state"] == "stopped"


def test_free_mode_is_unchanged(site):  # noqa: F811 - the default test config is "free"
    s = make(site, FakeRun(stdout="session " + UUID1))
    assert go(site, s, "alpha")["name"] == "alpha"
    dry = go(site, s, "beta-12", dry_run=True)["argv"]
    assert dry[dry.index("-n") + 1] == "beta-12"
    res = s.relaunch("alpha", handoff=str(_handoff(site)))
    assert res["name"] == "alpha" and "previous_name" not in res
    assert not (site.state / "sessions" / "_counters.json").exists()


def test_bad_session_names_config_refused(site):  # noqa: F811
    with pytest.raises(ParameterRefused, match="session_names"):
        go(_mode(site, "nope"), make(site), "alpha")


def test_corrupt_counters_file_refuses(rc):
    s = make(rc, FakeRun(stdout="session " + UUID1))
    go(rc, s, "alpha")
    (rc.state / "sessions" / "_counters.json").write_text("[1]")
    with pytest.raises(ParameterRefused, match="_counters.json"):
        go(rc, s, "alpha")


_CHILD = """import sys, time, types
from plangraph.spawner.sessions import Sessions
from sessions_support import MODEL, EFFORT, MODE, UUID1
state, cfg, cwd, mcp = sys.argv[1:5]
def run(argv, **kw):
    time.sleep(0.3)  # widen the read-next -> write window
    return types.SimpleNamespace(returncode=0, stdout="session " + UUID1, stderr="")
s = Sessions(state_root=state, config_path=cfg, run=run, sleep=lambda _s: None)
print(s.launch(name="eps", cwd=cwd, model=MODEL, effort=EFFORT, permission_mode=MODE,
               mcp_config=mcp, prompt="do it")["name"])
"""


def test_processes_get_distinct_numbers(rc):
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    procs = [subprocess.Popen(
        [sys.executable, "-c", _CHILD, str(rc.state), str(rc.cfg), str(rc.proj), str(rc.mcp)], env=env, stdout=subprocess.PIPE, text=True) for _ in range(4)]
    out = [p.communicate(timeout=60)[0].strip() for p in procs]
    assert {p.returncode for p in procs} == {0}
    assert sorted(out) == [f"eps-{n:04d}" for n in range(1, 5)]


def test_relaunch_of_a_superseded_record_is_refused(rc):
    run = FakeRun(stdout="session " + UUID1)
    s = make(rc, run)
    go(rc, s, "alpha")
    hf = _handoff(rc)
    run.stdout = "session " + UUID2
    assert s.relaunch("alpha-0001", handoff=str(hf))["name"] == "alpha-0002"
    tip = rc.state / "sessions" / "alpha-0002.json"
    before, calls = tip.read_text(), len(run.calls)
    with pytest.raises(ParameterRefused, match="alpha-0001 is superseded by alpha-0002: relaunch alpha-0002"):
        s.relaunch("alpha-0001", handoff=str(hf))
    assert tip.read_text() == before and len(run.calls) == calls  # the live tip untouched, nothing run
    assert _names(rc) == ["alpha-0001", "alpha-0002"]  # no number was taken


def test_free_mode_counters_name_does_not_block_role_counter(site):  # noqa: F811
    s = make(site, FakeRun(stdout="session " + UUID1))
    names = [go(site, s, "counters")["name"], go(_mode(site, "role-counter"), s, "alpha")["name"]]
    assert names == ["counters", "alpha-0001"]  # "counters" is a valid free name
    assert [x["name"] for x in s.list()["sessions"]] == ["alpha-0001", "counters"]


def test_concurrent_relaunches_of_one_role_do_not_both_start(rc):
    s = make(rc, FakeRun(stdout="session " + UUID1))
    go(rc, s, "alpha")
    hf = str(_handoff(rc))
    assert s.relaunch("alpha-0001", handoff=hf)["name"] == "alpha-0002"  # 0001 stopped, 0002 live
    real, inside = s._relaunch_record, threading.Event()

    def slow(*args):  # the window between stop and start
        inside.set()
        time.sleep(0.3)
        return real(*args)

    s._relaunch_record = slow
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(s.relaunch, "alpha-0002", hf)
        assert inside.wait(5)
        second = pool.submit(s.relaunch, "alpha-0001", hf)
        with pytest.raises(ParameterRefused, match="alpha-0001 is superseded by alpha-0003"):
            second.result()
        assert first.result()["name"] == "alpha-0003"
    assert _names(rc) == ["alpha-0001", "alpha-0002", "alpha-0003"]


def test_stop_waits_for_a_launch_in_flight(rc):
    run, started = FakeRun(stdout="session " + UUID1), threading.Event()

    def slow(argv, **kw):
        if argv[:2] == ["claude", "--bg"]:
            started.set()
            time.sleep(0.3)  # the record reads "launching" meanwhile
        return run(argv, **kw)

    s = make(rc, slow)
    with ThreadPoolExecutor(1) as pool:
        launch = pool.submit(go, rc, s, "alpha")
        assert started.wait(5)
        assert (s.stop(name="alpha-0001")["session_id"], launch.result()["session_id"]) == (UUID1, UUID1)
    assert json.loads((rc.state / "sessions" / "alpha-0001.json").read_text())["state"] == "stopped"
