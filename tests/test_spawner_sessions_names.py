"""tests/test_spawner_sessions_names.py - D-3605: <role>-NNNN session names."""

from __future__ import annotations

import json
import threading
import pytest
from plangraph.routing_errors import ParameterRefused
from sessions_support import UUID1, UUID2, PROMPT, FakeRun, make, go, write_config
from sessions_support import site  # noqa: F401


@pytest.fixture
def rc(site):  # noqa: F811
    """The site config switched to the default role-counter mode."""
    cfg = json.loads(site.cfg.read_text())
    cfg["session_names"] = "role-counter"
    write_config(site.cfg, json.dumps(cfg))
    return site


def _names(site):
    return sorted(p.stem for p in (site.state / "sessions").glob("*-????.json"))


def test_counter_starts_at_0001_and_increments(rc):
    s = make(rc, FakeRun(stdout="session " + UUID1))
    assert go(rc, s, "alpha")["name"] == "alpha-0001"
    assert go(rc, s, "alpha")["name"] == "alpha-0002"
    assert go(rc, s, "beta")["name"] == "beta-0001"
    assert _names(rc) == ["alpha-0001", "alpha-0002", "beta-0001"]
    counters = json.loads((rc.state / "sessions" / "_counters.json").read_text())
    assert counters == {"alpha": 2, "beta": 1}
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
    with pytest.raises(ParameterRefused, match="expected alpha-0002"):
        go(rc, s, "alpha-0007")
    with pytest.raises(ParameterRefused, match="expected alpha-0002"):
        go(rc, s, "alpha-0001")
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
    got, errors = [], []

    def work():
        try:
            got.append(go(rc, s, "gamma")["name"])
        except Exception as exc:  # noqa: BLE001 - surfaced by the assert below
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert sorted(got) == [f"gamma-{n:04d}" for n in range(1, 9)]


def test_relaunch_starts_a_new_numbered_session(rc):
    run = FakeRun(stdout="session " + UUID1)
    s = make(rc, run)
    go(rc, s, "alpha")
    sessions = rc.state / "sessions"
    hf = rc.tmp_path / "handoff.md"
    hf.write_text("carry on")
    s.mark_ready("alpha-0001", str(hf))
    run.stdout = "session " + UUID2
    res = s.relaunch("alpha-0001")
    assert res["name"] == "alpha-0002" and res["previous_name"] == "alpha-0001"
    assert res["session_id"] == UUID2
    old = json.loads((sessions / "alpha-0001.json").read_text())
    new = json.loads((sessions / "alpha-0002.json").read_text())
    assert old["state"] == "stopped" and old["session_id"] == UUID1 and old["name"] == "alpha-0001"
    assert new["name"] == "alpha-0002" and new["session_id"] == UUID2 and new["state"] == "running"
    assert new["handoff"] == str(hf.resolve())
    assert new["prompt_file"] == str(sessions / "alpha-0002.prompt.txt")
    assert (sessions / "alpha-0002.prompt.txt").read_text() == PROMPT
    assert (sessions / "alpha-0001.prompt.txt").read_text() == PROMPT
    assert not (sessions / "alpha-0001.ready.json").exists()
    assert run.launch_calls()[1][-1] == f"Restart from handoff {hf.resolve()}.\n\n" + PROMPT
    assert go(rc, s, "alpha")["name"] == "alpha-0003"  # relaunch took 0002
    s.stop(name="alpha-0003")  # a later live record of the role would supersede 0002
    res2 = s.relaunch("alpha-0002", handoff=str(hf))
    assert res2["name"] == "alpha-0004" and res2["previous_name"] == "alpha-0002"


def test_relaunch_of_a_legacy_name_gets_a_numbered_session(rc):
    run = FakeRun(stdout="session " + UUID1)
    s = make(rc, run, now=lambda: 1000.0)
    write_config(rc.cfg, json.dumps({**json.loads(rc.cfg.read_text()), "session_names": "free"}))
    go(rc, s, "alpha")
    write_config(rc.cfg, json.dumps({**json.loads(rc.cfg.read_text()), "session_names": "role-counter"}))
    hf = rc.tmp_path / "handoff.md"
    hf.write_text("carry on")
    res = s.relaunch("alpha", handoff=str(hf))
    assert res["name"] == "alpha-0001" and res["previous_name"] == "alpha"
    assert json.loads((rc.state / "sessions" / "alpha.json").read_text())["state"] == "stopped"


def test_free_mode_is_unchanged(site):  # noqa: F811 - the default test config is "free"
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    assert go(site, s, "alpha")["name"] == "alpha"
    dry = go(site, s, "beta-12", dry_run=True)["argv"]
    assert dry[dry.index("-n") + 1] == "beta-12"
    hf = site.tmp_path / "handoff.md"
    hf.write_text("carry on")
    res = s.relaunch("alpha", handoff=str(hf))
    assert res["name"] == "alpha" and "previous_name" not in res
    assert not (site.state / "sessions" / "_counters.json").exists()


def test_bad_session_names_config_refused(site):  # noqa: F811
    write_config(site.cfg, json.dumps({**json.loads(site.cfg.read_text()), "session_names": "nope"}))
    with pytest.raises(ParameterRefused, match="session_names"):
        go(site, make(site), "alpha")


def test_corrupt_counters_file_refuses(rc):
    s = make(rc, FakeRun(stdout="session " + UUID1))
    go(rc, s, "alpha")
    (rc.state / "sessions" / "_counters.json").write_text("[1]")
    with pytest.raises(ParameterRefused, match="_counters.json"):
        go(rc, s, "alpha")


_CHILD = """
import json, sys, time, types
from plangraph.spawner.sessions import Sessions
state, cfg, cwd, mcp, model, effort, mode = sys.argv[1:8]
def run(argv, **kw):
    if argv[1:3] == ["agents", "--json"]:
        return types.SimpleNamespace(returncode=0, stdout="[]", stderr="")
    time.sleep(0.3)  # widen the read-next -> write window
    return types.SimpleNamespace(returncode=0, stdout="session %s" % sys.argv[8], stderr="")
s = Sessions(state_root=state, config_path=cfg, run=run, sleep=lambda _s: None)
print(s.launch(name="eps", cwd=cwd, model=model, effort=effort, permission_mode=mode,
               mcp_config=mcp, prompt="do it")["name"])
"""


def test_processes_get_distinct_numbers(rc):
    import os
    import subprocess
    import sys
    from sessions_support import MODEL, EFFORT, MODE
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    procs = [subprocess.Popen(
        [sys.executable, "-c", _CHILD, str(rc.state), str(rc.cfg), str(rc.proj), str(rc.mcp),
         MODEL, EFFORT, MODE, UUID1], env=env, stdout=subprocess.PIPE, text=True) for _ in range(4)]
    out = [p.communicate(timeout=60)[0].strip() for p in procs]
    assert [p.returncode for p in procs] == [0, 0, 0, 0]
    assert sorted(out) == [f"eps-{n:04d}" for n in range(1, 5)]


def test_relaunch_of_a_superseded_record_is_refused(rc):
    run = FakeRun(stdout="session " + UUID1)
    s = make(rc, run)
    go(rc, s, "alpha")
    hf = rc.tmp_path / "handoff.md"
    hf.write_text("carry on")
    run.stdout = "session " + UUID2
    assert s.relaunch("alpha-0001", handoff=str(hf))["name"] == "alpha-0002"
    before = (rc.state / "sessions" / "alpha-0002.json").read_text()
    stops, launches = len([c for c in run.calls if c[0][1:2] == ["stop"]]), len(run.launch_calls())
    with pytest.raises(ParameterRefused, match="alpha-0001 is superseded by alpha-0002: relaunch alpha-0002"):
        s.relaunch("alpha-0001", handoff=str(hf))
    assert (rc.state / "sessions" / "alpha-0002.json").read_text() == before  # the live tip is untouched
    assert len([c for c in run.calls if c[0][1:2] == ["stop"]]) == stops and len(run.launch_calls()) == launches
    assert _names(rc) == ["alpha-0001", "alpha-0002"]  # no number was taken


def test_free_mode_counters_name_does_not_block_role_counter(site):  # noqa: F811
    s = make(site, FakeRun(stdout="session " + UUID1))
    assert go(site, s, "counters")["name"] == "counters"  # a valid free name
    write_config(site.cfg, json.dumps({**json.loads(site.cfg.read_text()), "session_names": "role-counter"}))
    assert go(site, s, "alpha")["name"] == "alpha-0001"
    assert [x["name"] for x in s.list()["sessions"]] == ["alpha-0001", "counters"]
