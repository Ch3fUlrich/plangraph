"""Shared shadow-gate evidence and scoped effect guards, registered by conftest."""
import builtins
import copy
import datetime
import io
import os
import socket
import subprocess
import time
import urllib.request

import pytest

SHA = "a" * 40


def facts():
    required = ["ci-gate", "select", "scrub", "file-size", "new-code-lang",
                "changelog-docs", "pytest", "pytest-postgres", "rust", "rust-worker", "rust-spike"]
    return dict(head_sha_at_check=SHA, head_sha_now=SHA,
                required_checks=required,
                ci_checks=[dict(name=name, head_sha=SHA, status="completed",
                                conclusion="success") for name in required],
                seats=[dict(family="xiaomi", verdict="VERDICT: ACCEPT",
                            pinned=False, open_findings=[])],
                writer_families=["openai"], changed_paths=["src/example.rs"],
                tier="standard", plan_key="demo:ITEM", item_key="demo:ITEM",
                preflight_present=True, main_changed_production_paths=[])


def refuse(core, facts, code):
    before = copy.deepcopy(facts)
    result = core.decide(facts)
    assert result.outcome == "REFUSE" and result.code == code
    assert result.detail and len(result.detail.splitlines()) == 1
    assert facts == before


def no_effects(monkeypatch, core, merge_gate):
    def forbidden(*args, **kwargs):
        pytest.fail("shadow decision attempted an external effect")
    for module, names in (
        (subprocess, ("Popen", "run", "call", "check_call", "check_output",
                      "getoutput", "getstatusoutput")),
        (os, ("system", "popen")),
        (socket, ("socket", "create_connection", "create_server")),
        (urllib.request, ("urlopen",)), (core.seat_verdict, ("send",)),
    ):
        for name in names:
            monkeypatch.setattr(module, name, forbidden)
    real_decide = core.decide

    class NoEnvironment:
        get = __getitem__ = __setitem__ = __iter__ = __contains__ = forbidden

    class NoDatetime(datetime.datetime):
        now = utcnow = today = forbidden

    class NoDate(datetime.date):
        today = forbidden

    def guarded_decide(facts):
        # Pytest and the CLI may use files/environment/clocks outside decide().
        with monkeypatch.context() as guard:
            guard.setattr(builtins, "open", forbidden)
            guard.setattr(io, "open", forbidden)
            guard.setattr(os, "open", forbidden)
            guard.setattr(os, "environ", NoEnvironment())
            guard.setattr(os, "getenv", forbidden)
            for name in ("time", "time_ns", "monotonic", "monotonic_ns", "perf_counter"):
                guard.setattr(time, name, forbidden)
            guard.setattr(datetime, "datetime", NoDatetime)
            guard.setattr(datetime, "date", NoDate)
            return real_decide(facts)

    monkeypatch.setattr(core, "decide", guarded_decide)
    monkeypatch.setattr(merge_gate, "decide", guarded_decide)
