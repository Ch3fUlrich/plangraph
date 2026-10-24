"""Fixtures for the keeper contract suite (see ``contract_harness`` for modes)."""
import json
import os
import statistics
from pathlib import Path

import pytest

import contract_harness as harness


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    for item in items:
        fspath = str(getattr(item, "fspath", "") or getattr(item, "path", ""))
        if "tests/contract" in fspath.replace("\\", "/"):
            item.add_marker(pytest.mark.xdist_group("contract"))


@pytest.fixture(scope="session")
def kp(tmp_path_factory):
    """The keeper under test: ``PG_CONTRACT_URL`` if set, else a local daemon."""
    external = harness.from_env()
    if external is not None:
        missing = [n for n in harness.PRINCIPALS if n not in external.tokens]
        if missing:
            pytest.fail(f"PG_CONTRACT_URL set but tokens missing for {missing}")
        yield external
        return
    keeper = harness.start_local(tmp_path_factory.mktemp("keeper") / "k")
    try:
        yield keeper
    finally:
        keeper.stop()


def _latency_dir():
    value = os.environ.get("PG_CONTRACT_LATENCY_DIR")
    return Path(value) if value else None


def pytest_sessionfinish(session, exitstatus):
    """Each xdist worker drops its per-request timings for ``pytest_terminal_summary``."""
    out = _latency_dir()
    if out is None or not harness.LATENCY:
        return
    out.mkdir(parents=True, exist_ok=True)
    name = getattr(session.config, "workerinput", {}).get("workerid", "main")
    (out / f"latency-{name}.json").write_text(json.dumps(harness.LATENCY), encoding="utf-8")


def pytest_terminal_summary(terminalreporter):
    """Per-route keeper latency (client side, loopback): count, p50, p95, max in ms, timeouts."""
    out = _latency_dir()
    if out is None or not out.is_dir():
        return
    rows = [r for f in sorted(out.glob("latency-*.json")) for r in json.loads(f.read_text())]
    if not rows:
        return
    by_route: dict[str, list] = {}
    for method, route, secs, status in rows:
        by_route.setdefault(f"{method} {route}", []).append((secs * 1000, status))
    write = terminalreporter.write_line
    terminalreporter.section("keeper request latency (ms)")
    write(f"{'route':<34}{'n':>6}{'p50':>8}{'p95':>8}{'max':>9}{'timeouts':>9}")
    for key, vals in sorted(by_route.items(), key=lambda kv: -max(v[0] for v in kv[1]))[:25]:
        ms = sorted(v[0] for v in vals)
        p95 = ms[min(len(ms) - 1, int(len(ms) * 0.95))]
        write(f"{key:<34}{len(ms):>6}{statistics.median(ms):>8.0f}{p95:>8.0f}{ms[-1]:>9.0f}"
              f"{sum(1 for v in vals if v[1] == 0):>9}")
    allms = sorted(v[0] for vals in by_route.values() for v in vals)
    write(f"{'ALL':<34}{len(allms):>6}{statistics.median(allms):>8.0f}"
          f"{allms[min(len(allms) - 1, int(len(allms) * 0.95))]:>8.0f}{allms[-1]:>9.0f}")
