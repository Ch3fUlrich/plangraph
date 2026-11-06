"""Windows collection exclusions must not change other hosts."""
from win_skips import NOFOLLOW, skip_reason


def test_inert_off_windows():
    for platform in ("linux", "darwin", "freebsd"):
        assert skip_reason("tests/test_spawner_utility_props.py::test_nan_terminates", platform, ()) is None
        for nodeid in NOFOLLOW:
            assert skip_reason(nodeid, platform, ("fake_ownership",)) is None


def test_alarm_facility_named():
    assert "SIGALRM" in skip_reason(
        "tests/test_spawner_utility_props.py::test_nan_terminates", "win32", ())


def test_proc_fixture_facility_named():
    assert "/proc" in skip_reason("tests/example.py::test_example", "win32", ("fake_ownership",))


def test_unlisted_windows_test_runs():
    assert skip_reason("tests/example.py::test_example", "win32", ()) is None


def test_utility_without_alarm_still_runs():
    assert skip_reason(
        "tests/test_spawner_utility_props.py::test_price_cliff_invalid_nan_raises",
        "win32", ()) is None


def test_nofollow_reason_and_parameter_matching():
    assert len(NOFOLLOW) == 132
    assert "tests/test_spawner_sessions.py::test_windows_which_resolution" not in NOFOLLOW
    for nodeid in NOFOLLOW:
        assert 'pins platform="linux"' in skip_reason(nodeid + "[case]", "win32", ())
