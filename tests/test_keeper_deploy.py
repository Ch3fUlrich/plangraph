"""keeper_deploy: smoke ok keeps the new drop-in; smoke fail restores the old set (fakes only)."""
import importlib.util
from pathlib import Path

import pytest

from plangraph.keeper.cli_verbs import RefusedCli

_spec = importlib.util.spec_from_file_location(
    "keeper_deploy", Path(__file__).resolve().parent.parent / "tools" / "keeper_deploy.py")
kd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(kd)


class FakeSession:
    def __init__(self, claim_error=None, missing_route=False):
        self.calls, self.claim_error, self.missing_route = [], claim_error, missing_route

    def call(self, method, verb, params=None, body=None):
        self.calls.append(verb)
        if verb == "claim" and self.claim_error:
            raise self.claim_error
        if verb == "close_merged" and self.missing_route:
            raise RefusedCli("not_found", "unknown path")
        if verb == "close_merged":
            raise RefusedCli("validation", "key required")
        return {"epoch": 3}


@pytest.fixture
def setup(tmp_path):
    d = tmp_path / "unit.d"
    d.mkdir()
    (d / "live-sync-old-aaaa.conf").write_text("OLD\n")
    ctl_calls = []
    return d, ctl_calls, lambda *a: ctl_calls.append(a)


def run(d, ctl, session):
    return kd.deploy("t1", "bbbb", "/tree/live-bbbb", "PROBE", session, dropin_dir=d, ctl=ctl,
                     timeout=1, sleep=lambda s: None)


def test_smoke_ok_keeps_new_dropin(setup):
    d, calls, ctl = setup
    session = FakeSession()
    assert run(d, ctl, session) == (0, "ok")
    assert (d / "live-sync-t1-bbbb.conf").exists()
    assert "PYTHONPATH=/tree/live-bbbb/src" in (d / "live-sync-t1-bbbb.conf").read_text()
    assert session.calls == ["health", "claim", "release", "close_merged"]
    assert calls == [("daemon-reload",), ("restart", kd.UNIT)]


def test_smoke_fail_restores_previous_and_restarts(setup):
    d, calls, ctl = setup
    err = RefusedCli("bad_state", "cannot read decisions file: x")
    code, msg = run(d, ctl, FakeSession(claim_error=err))
    assert code == 1 and "cannot read decisions file" in msg and "restored" in msg
    assert sorted(p.name for p in d.iterdir()) == ["live-sync-old-aaaa.conf"]
    assert (d / "live-sync-old-aaaa.conf").read_text() == "OLD\n"
    assert calls == [("daemon-reload",), ("restart", kd.UNIT)] * 2


def test_missing_close_merged_route_fails(setup):
    d, _, ctl = setup
    code, msg = run(d, ctl, FakeSession(missing_route=True))
    assert code == 1 and "close_merged route missing" in msg
    assert not (d / "live-sync-t1-bbbb.conf").exists()


def test_restart_error_rolls_back(setup):
    d, calls, _ = setup

    def ctl(*a):
        calls.append(a)
        if a[0] == "restart" and calls.count(a) == 1:
            raise RuntimeError("restart failed")

    code, msg = run(d, ctl, FakeSession())
    assert code == 1 and "restart failed" in msg
    assert not (d / "live-sync-t1-bbbb.conf").exists()
