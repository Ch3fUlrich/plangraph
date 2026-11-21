"""The affected-tests bound scales with the selection (LE-PREPUSH-AFFECTED-BOUND-SPAWNER-CORE)."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import types

import pytest

from plangraph.spawner import proc_kill
from test_preflight import preflight, repo  # noqa: F401


ROOT = Path(__file__).resolve().parents[1]


def plan_script(tmp_path, count, body=''):
    script = tmp_path / 'affected_tests.py'
    script.write_text('import json, sys, time\n'
                      f'print(json.dumps({{"tests": ["t%d" % i for i in range({count})]}}), flush=True)\n'
                      'if "--run" in sys.argv:\n'
                      f'    {body or "pass"}\n')
    return script


@pytest.mark.parametrize('count,expected', [
    (0, 240), (100, 240), (120, 240), (223, 446), (570, 1140), (5000, preflight.MAX_TEST_TIMEOUT)])
def test_bound_scales_with_selected_paths_and_is_capped(tmp_path, count, expected):
    script = plan_script(tmp_path, count)
    command = [sys.executable, str(script), '--run']
    assert preflight.check_timeout(command, tmp_path, {}) == expected


def test_a_command_without_run_or_with_an_unreadable_plan_keeps_the_base_bound(tmp_path):
    assert preflight.check_timeout([sys.executable, str(plan_script(tmp_path, 900))], tmp_path, {}) == preflight.TEST_TIMEOUT
    broken = tmp_path / 'affected_tests.py'
    broken.write_text('print("not json")\n')
    assert preflight.check_timeout([sys.executable, str(broken), '--run'], tmp_path, {}) == preflight.TEST_TIMEOUT


def test_a_slow_large_selection_passes_where_the_flat_bound_failed(tmp_path, monkeypatch):
    """A selection whose run outlasts the old flat bound still passes inside the scaled one."""
    monkeypatch.setattr(preflight, 'TEST_TIMEOUT', 0.5)
    monkeypatch.setattr(preflight, 'TEST_PER_PATH_TIMEOUT', 0.05)
    monkeypatch.setattr(preflight, 'MAX_TEST_TIMEOUT', 20)
    script = plan_script(tmp_path, 40, 'time.sleep(1.5)')
    code, _ = preflight.run([sys.executable, str(script), '--run'], tmp_path, dict(__import__('os').environ))
    assert code == 0   # bound = max(0.5, 40 * 0.05) = 2 s > 1.5 s run


def test_the_scaled_bound_is_named_when_it_is_exceeded(tmp_path, monkeypatch):
    monkeypatch.setattr(preflight, 'TEST_TIMEOUT', 0.5)
    monkeypatch.setattr(preflight, 'TEST_PER_PATH_TIMEOUT', 0.05)
    monkeypatch.setattr(preflight, 'MAX_TEST_TIMEOUT', 20)
    script = plan_script(tmp_path, 40, 'time.sleep(10)')
    code, text = preflight.run([sys.executable, str(script), '--run'], tmp_path, dict(__import__('os').environ))
    assert code == 1
    assert text == 'tests: affected-tests step exceeded its 2.0 second bound (40 selected test paths)\n'



def _memory(mb):
    def read():
        if isinstance(mb, Exception):
            raise mb
        return mb
    return read


@pytest.mark.parametrize('cores,memory,expected', [
    (32, 110 * 1024, 16), (32, 20 * 1024, 10), (12, 3891, 2), (2, 64 * 1024, 2),
    (8, PermissionError('no /proc'), 4), (8, ValueError('MemAvailable missing'), 4)])
def test_affected_workers_follow_cores_and_memory_never_fixed_at_two(cores, memory, expected):
    """LE-PREPUSH-TESTS-ON-BUSIEST-HOST (3): the worker count comes from the host, between 2 and 16;
    an unreadable memory probe falls back to the cores."""
    assert preflight.affected_workers({}, cores=cores, memory_mb=_memory(memory)) == expected


def test_affected_workers_override():
    assert preflight.affected_workers({'PREFLIGHT_TEST_WORKERS': '6'}, cores=32, memory_mb=_memory(110 * 1024)) == 6
    for bad in ('x', '0', '-3', '\u00b2', ''):
        assert preflight.affected_workers({'PREFLIGHT_TEST_WORKERS': bad}, cores=32, memory_mb=_memory(110 * 1024)) == 16


def test_memavailable_line_without_value_raises_valueerror(tmp_path, monkeypatch):
    """The shared probe answers ValueError for a malformed line, so preflight falls back to cores."""
    from plangraph.spawner import overflow
    meminfo = tmp_path / 'meminfo'
    meminfo.write_text('MemTotal: 1 kB\nMemAvailable:\n')
    real = overflow.Path
    monkeypatch.setattr(overflow, 'Path', lambda p: meminfo if str(p) == '/proc/meminfo' else real(p))
    # Only overflow sees a POSIX os.name; the global os module stays untouched, so pathlib works on Windows.
    monkeypatch.setattr(overflow, 'os', types.SimpleNamespace(name='posix'))
    with pytest.raises(ValueError):
        overflow.available_memory_mb()
    assert preflight.affected_workers({}, cores=12, memory_mb=overflow.available_memory_mb) == 6


@pytest.mark.skipif(not sys.platform.startswith("linux") or shutil.which("cargo") is None,
                    reason="requires Linux process inspection and Cargo")
@pytest.mark.test_deadline(preflight.RUST_TIMEOUT + 15, reason="the guard may need a cold Cargo build")
def test_push_gate_names_hung_rust_case_and_reaps_child(tmp_path):
    shutil.copyfile(ROOT / "rust-toolchain.toml", tmp_path / "rust-toolchain.toml")
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "push_gate_hang_fixture"\nversion = "0.0.0"\nedition = "2021"\n')
    (tmp_path / "src").mkdir()
    (tmp_path / "src/lib.rs").write_text(r"""
#[test]
fn hanging_case() {
    use std::{fs, process::{Command, Stdio}, thread, time::Duration};

    let mut child = Command::new("sleep")
        .arg("30")
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    let pid = child.id();
    let stat = fs::read_to_string(format!("/proc/{pid}/stat")).unwrap();
    let started = stat.rsplit_once(')').unwrap().1.split_whitespace().nth(19).unwrap();
    fs::write("child.identity", format!("{pid} {started}")).unwrap();
    // A broken guard produces a bounded test failure without leaving a sleep behind.
    thread::sleep(Duration::from_secs(4));
    child.kill().unwrap();
    child.wait().unwrap();
}
""")
    code, output = preflight.run(["cargo", "test", "--lib"], tmp_path,
                                 dict(os.environ, PLANGRAPH_TEST_DEADLINE="1"))
    if "no matching package named" in output and "offline mode" in output:
        pytest.skip("the offline Cargo cache does not contain the runner dependencies")
    identity = tmp_path / "child.identity"
    assert identity.exists(), output
    pid, started = map(int, identity.read_text().split())
    survived = proc_kill.alive(pid, started)
    if survived:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    assert code != 0, output
    assert "HUNG push_gate_hang_fixture::hanging_case" in output, output
    assert not survived, f"Rust test child {pid} survived the push gate"


def run_deadline_fixture(tmp_path, source, seconds, *pytest_args):
    shutil.copyfile(ROOT / "tests/conftest.py", tmp_path / "conftest.py")
    shutil.copyfile(ROOT / "pytest.ini", tmp_path / "pytest.ini")
    (tmp_path / "test_case.py").write_text(source)
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(map(str, (ROOT / "tests", ROOT / "src"))))
    for name in ("PYTEST_ADDOPTS", "PYTEST_SHARD", "PYTEST_CURRENT_TEST"):
        env.pop(name, None)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-o", f"test_deadline={seconds}",
         *pytest_args, "test_case.py"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=20)


@pytest.mark.parametrize(("decorated_test", "seconds", "diagnostic"), [
    pytest.param("@pytest.mark.test_deadline(5)\ndef test_case(): pass\n",
                 "1", "requires a named test and reason", id="no-reason"),
    pytest.param('@pytest.mark.test_deadline(5, reason="class budget")\n'
                 'class TestGroup:\n    def test_case(self): pass\n',
                 "1", "requires a named test and reason", id="class-mark"),
    pytest.param('@pytest.mark.test_deadline(0, reason="off")\ndef test_case(): pass\n',
                 "1", "must be finite and greater than zero", id="zero-mark"),
    pytest.param('@pytest.mark.test_deadline(float("inf"), reason="unbounded")\n'
                 'def test_case(): pass\n',
                 "1", "must be finite and greater than zero", id="infinite-mark"),
    pytest.param("def test_case(): pass\n",
                 "0", "must be finite and greater than zero", id="zero-ini"),
    pytest.param("def test_case(): pass\n",
                 "nan", "must be finite and greater than zero", id="nan-ini"),
])
def test_deadline_invalid_configuration_is_refused(tmp_path, decorated_test, seconds, diagnostic):
    result = run_deadline_fixture(tmp_path, "import pytest\n" + decorated_test, seconds,
                                  "--collect-only")
    assert result.returncode == pytest.ExitCode.USAGE_ERROR, result.stdout + result.stderr
    assert "ERROR: test_case.py::" in result.stderr and diagnostic in result.stderr


def test_named_deadline_extends_the_ini_limit(tmp_path):
    result = run_deadline_fixture(tmp_path, """import time
import pytest

@pytest.mark.test_deadline(10, reason="this fixture exceeds the ini deadline")
def test_case():
    time.sleep(2)
""", "0.1")
    assert result.returncode == pytest.ExitCode.OK, result.stdout + result.stderr
    assert "1 passed" in result.stdout


@pytest.mark.parametrize(('fixture_pause', 'test_pause', 'hung'), [
    (0.8, 0, False),
    (0.8, 5, True),
    (5, 0, True),
])
def test_cargo_fixture_has_a_separate_deadline(tmp_path, fixture_pause, test_pause, hung):
    result = run_deadline_fixture(tmp_path, f"""
import pytest
import time
from _split_ci_environment import cargo_fixture_deadline

@pytest.fixture
def binary():
    with cargo_fixture_deadline(2):
        time.sleep({fixture_pause})

def test_case(binary):
    time.sleep({test_pause})
""", '0.5')
    if hung:
        assert result.returncode != pytest.ExitCode.OK, result.stdout + result.stderr
        suffix = ' [Cargo fixture]' if fixture_pause > 2 else ''
        assert f'HUNG test_case.py::test_case{suffix}\n' in result.stderr
    else:
        assert result.returncode == pytest.ExitCode.OK, result.stdout + result.stderr
        assert '1 passed' in result.stdout
