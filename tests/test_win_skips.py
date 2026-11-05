"""Windows collection exclusions must not change other hosts."""
from win_skips import skip_reason


def test_inert_off_windows():
    for platform in ("linux", "darwin", "freebsd"):
        assert skip_reason("tests/test_spawner_utility_props.py::test_nan_terminates", platform, ()) is None


def test_alarm_facility_named():
    assert "SIGALRM" in skip_reason(
        "tests/test_spawner_utility_props.py::test_nan_terminates", "win32", ())


def test_proc_fixture_facility_named():
    assert "/proc" in skip_reason("tests/example.py::test_example", "win32", ("fake_ownership",))


def test_unlisted_windows_test_runs():
    assert skip_reason("tests/example.py::test_example", "win32", ()) is None
