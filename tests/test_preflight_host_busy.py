"""An affected-test timeout on a loaded host carries a machine-readable host_busy reason."""
import json
import re
import subprocess
from pathlib import Path

import pytest

from test_preflight import preflight, repo  # noqa: F401
import ci_select

ROOT = Path(__file__).resolve().parents[1]


def times_out(monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"], output=b'{"tests": ["a"]}\n')

    monkeypatch.setattr(preflight, "run_test_process", timeout)


def reason_lines(text):
    rows = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and "reason" in row:
            rows.append(row)
    return rows


@pytest.mark.parametrize("load,cores,busy", [
    (5.59, 8, False), (5.6, 8, True), (9.0, 8, True), (0.0, 8, False), (3.0, 0, False)])
def test_busy_threshold_is_point_seven_load_per_cpu(load, cores, busy):
    assert (ci_select.host_busy_reason(load, cores) is not None) is busy


def test_timeout_on_a_loaded_host_prints_the_host_busy_reason(monkeypatch, repo):
    times_out(monkeypatch)
    monkeypatch.setattr(preflight, "host_load", lambda: (7.0, 8))
    code, text = preflight.run(["python3", "tools/affected_tests.py"], repo, {})
    assert code == 1
    assert text.startswith("tests: affected-tests step exceeded its ")
    [row] = reason_lines(text)
    assert row["reason"] == "host_busy"
    assert row["cpus"] == 8 and row["load_per_cpu"] == 0.88


def test_timeout_on_an_idle_host_stays_a_plain_failure(monkeypatch, repo):
    times_out(monkeypatch)
    monkeypatch.setattr(preflight, "host_load", lambda: (1.0, 8))
    _, text = preflight.run(["python3", "tools/affected_tests.py"], repo, {})
    assert reason_lines(text) == []


def test_a_loaded_host_with_a_real_test_failure_prints_no_reason(monkeypatch, repo):
    monkeypatch.setattr(preflight, "host_load", lambda: (9.0, 8))
    monkeypatch.setattr(preflight, "run_test_process",
                        lambda *a, **k: subprocess.CompletedProcess(a[0], 1, "FAILED tests/test_x.py::t\n"))
    code, text = preflight.run(["python3", "tools/affected_tests.py"], repo, {})
    assert code == 1 and reason_lines(text) == []


def test_python_and_rust_agree_on_the_threshold_and_the_reason_name():
    source = (ROOT / "rust/plangraph-cli/src/fast_check_dispatch.rs").read_text()
    threshold = re.search(r"MAX_LOAD_PER_CPU: f64 = ([0-9.]+);", source).group(1)
    reason = re.search(r'HOST_BUSY: &str = "(\w+)";', source).group(1)
    assert float(threshold) == ci_select.HOST_BUSY_LOAD_PER_CPU
    assert reason == ci_select.HOST_BUSY_REASON
