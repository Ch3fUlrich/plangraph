"""Changed keeper test files also run on Postgres in the fast checks (LE-KEEPER-TESTS-NEVER-RUN-ON-POSTGRES-BEFORE-CI)."""
import importlib.util
import os
import stat
import sys
from pathlib import Path
from types import ModuleType


def selector():
    spec = importlib.util.spec_from_file_location(
        "affected_tests_pg", Path(__file__).resolve().parents[1] / "tools/affected_tests.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def stub_bin(tmp_path, log):
    """A directory of fake Postgres tools that log their argv and succeed."""
    directory = tmp_path / "pgbin"
    directory.mkdir()
    for name in ("initdb", "pg_ctl", "createdb", "psql"):
        tool = directory / name
        tool.write_text(f'#!/bin/sh\necho "{name} $*" >> {log}\n')
        tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    return directory


def test_only_changed_or_added_keeper_test_files_are_chosen(tmp_path):
    (tmp_path / "tests").mkdir()
    for name in ("test_keeper_a.py", "test_keeper_b.py", "test_other.py", "keeper_util.py"):
        (tmp_path / "tests" / name).write_text("")
    chosen = selector().keeper_postgres_files(
        tmp_path, ["tests/test_keeper_a.py", "tests/test_other.py", "tests/test_keeper_gone.py",
                   "tests/keeper_util.py", "src/plangraph/keeper/db.py"])
    assert chosen == ["tests/test_keeper_a.py"]


def test_postgres_tools_come_from_pg_bin_then_the_path(tmp_path):
    log = tmp_path / "log"
    directory = stub_bin(tmp_path, log)
    mod = selector()
    assert mod.postgres_bin({"PG_BIN": str(directory), "PATH": ""}) == directory
    assert mod.postgres_bin({"PATH": str(directory)}) == directory
    assert mod.postgres_bin({"PATH": str(tmp_path / "nothing"), "PG_BIN": ""}, search=()) is None


def test_a_host_without_postgres_says_so_and_does_not_fail(tmp_path, capsys):
    mod = selector()
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_keeper_a.py").write_text("")
    code = mod.run_keeper_postgres(tmp_path, ["tests/test_keeper_a.py"],
                                   {"PATH": "", "HOME": str(tmp_path)}, search=())
    assert code == 0
    assert "keeper-postgres: SKIPPED: no Postgres binaries on this host" in capsys.readouterr().out


def test_optional_postgres_run_skips_without_psycopg(tmp_path, capsys, monkeypatch):
    monkeypatch.setitem(sys.modules, "psycopg", None)
    log = tmp_path / "log"
    directory = stub_bin(tmp_path, log)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_keeper_a.py").write_text("")
    started = []

    def runner(command, **kwargs):
        started.append(command)
        return type("R", (), {"returncode": 3})()

    code = selector().run_keeper_postgres(
        tmp_path, ["tests/test_keeper_a.py"],
        {"PG_BIN": str(directory), "PATH": os.environ["PATH"]}, runner=runner)
    assert code == 0
    assert "keeper-postgres: SKIPPED: Python cannot find psycopg" in capsys.readouterr().out
    assert not started and not log.exists()


def test_the_files_run_with_the_postgres_backend_on_a_throwaway_cluster(tmp_path, monkeypatch):
    driver = ModuleType("psycopg")
    driver.__spec__ = importlib.util.spec_from_loader("psycopg", loader=None)
    monkeypatch.setitem(sys.modules, "psycopg", driver)
    log = tmp_path / "log"
    directory = stub_bin(tmp_path, log)
    mod = selector()
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_keeper_a.py").write_text("")
    seen = {}

    def runner(command, **kwargs):
        seen["command"], seen["env"] = command, kwargs["env"]
        return type("R", (), {"returncode": 3})()

    code = mod.run_keeper_postgres(tmp_path, ["tests/test_keeper_a.py"],
                                   {"PG_BIN": str(directory), "PATH": os.environ["PATH"]}, runner=runner)
    assert code == 3                                            # a failing Postgres run fails the checks
    assert seen["command"][-1] == "tests/test_keeper_a.py"
    assert seen["env"]["PG_KEEPER_BACKEND"] == "postgres"
    assert seen["env"]["PG_DSN"].startswith("postgresql://postgres@/keeper?host=")
    calls = log.read_text()
    assert "initdb" in calls and "pg_ctl" in calls and "createdb" in calls
    assert calls.rstrip().splitlines()[-1].startswith("pg_ctl") and "stop" in calls.splitlines()[-1]


def test_an_existing_pg_dsn_is_used_without_starting_a_cluster(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "psycopg", None)
    mod = selector()
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_keeper_a.py").write_text("")
    seen = {}

    def runner(command, **kwargs):
        seen["env"] = kwargs["env"]
        return type("R", (), {"returncode": 0})()

    assert mod.run_keeper_postgres(tmp_path, ["tests/test_keeper_a.py"],
                                   {"PG_DSN": "postgresql://x@h/keeper", "PATH": ""}, runner=runner) == 0
    assert seen["env"]["PG_DSN"] == "postgresql://x@h/keeper"


def test_nothing_changed_means_no_postgres_run(tmp_path, capsys):
    assert selector().run_keeper_postgres(tmp_path, ["src/x.py"], {}) == 0
    assert capsys.readouterr().out == ""
