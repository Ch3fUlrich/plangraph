"""Single-successor relaunch guard tests."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from plangraph.routing_errors import ParameterRefused
from sessions_support import UUID1, UUID2, FakeRun, make, go
from sessions_support import site  # noqa: F401


@pytest.mark.parametrize("concurrent", [False, True])
def test_same_handoff_has_one_successor(site, monkeypatch, concurrent):
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    go(site, s)
    # Keep the legacy source record while relaunch creates numbered successors.
    cfg = json.loads(site.cfg.read_text())
    cfg["session_names"] = "role-counter"
    site.cfg.write_text(json.dumps(cfg))
    hf = site.tmp_path / "handoff.md"
    hf.write_text("carry on")
    s.mark_ready("alpha", str(hf))
    run.stdout = "session " + UUID2
    entered, release = threading.Event(), threading.Event()
    start = s._start

    def blocked_start(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return start(*args, **kwargs)

    if concurrent:
        monkeypatch.setattr(s, "_start", blocked_start)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(s.relaunch, "alpha")
            try:
                assert entered.wait(5)
                # A different instance must see the same cross-process lock.
                second = pool.submit(make(site, run).relaunch, "alpha", str(hf))
                with pytest.raises(ParameterRefused, match="already relaunched from this handoff"):
                    second.result(timeout=5)
            finally:
                release.set()
            assert first.result(timeout=5)["ok"]
    else:
        assert s.relaunch("alpha")["ok"]
    with pytest.raises(ParameterRefused, match="already relaunched from this handoff"):
        s.relaunch("alpha", str(hf))
    assert len(run.launch_calls()) == 2
    records = s._all_records()
    assert {r["name"] for r in records} == {"alpha", "alpha-0001"}
    assert sum(r.get("relaunch_handoff") == str(hf.resolve()) for r in records) == 1
