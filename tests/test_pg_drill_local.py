"""tools/pg-drill-local.sh: hermetic drill coverage with fake pg_dump/docker/psql/pg_restore/pg_isready."""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "pg-drill-local.sh"
BASH = shutil.which("bash") or "/bin/bash"

pytestmark = pytest.mark.skipif(os.name != "posix", reason="pg-drill-local.sh is a posix bash script")

DOCKER_FAKE = """\
#!/bin/bash
TMP="{tmp}"
l=docker; for a in "$@"; do l="$l $a"; done; printf '%s\\n' "$l" >>"$TMP/calls.log"
case "${{1:-}}" in
  run) touch "$TMP/container" ;;
  rm) rm -f "$TMP/container" ;;
esac
exit 0
"""

PSQL_FAKE = """\
#!/bin/bash
TMP="{tmp}"
l=psql; for a in "$@"; do l="$l $a"; done; printf '%s\\n' "$l" >>"$TMP/calls.log"
dsn=""; query=""; tbl=""; prev=""
for a in "$@"; do
  case "$prev" in
    -c) query="$a" ;;
    -v) [ "${{a#tbl=}}" != "$a" ] && tbl="${{a#tbl=}}" ;;
  esac
  case "$a" in
    postgresql://*) dsn="$a" ;;
  esac
  prev="$a"
done
db="${{dsn##*/}}"
case "$query" in
  *"CREATE DATABASE"*) exit 0 ;;
  *information_schema*)
    if [ "$db" = keeper ]; then var=FAKE_TABLES_SRC; else var=FAKE_TABLES_RESTORE; fi
    tables="${{!var}}"
    old_ifs="$IFS"; IFS=' '
    for t in $tables; do printf '%s\\n' "$t"; done
    IFS="$old_ifs"
    exit 0 ;;
  *"count(*)"*)
    if [ "$db" = keeper ]; then var=FAKE_COUNTS_SRC; else var=FAKE_COUNTS_RESTORE; fi
    for pair in ${{!var}}; do
      if [ "${{pair%%=*}}" = "$tbl" ]; then printf '%s\\n' "${{pair#*=}}"; exit 0; fi
    done
    printf '0\\n'
    exit 0 ;;
esac
exit 0
"""

PG_DUMP_FAKE = """\
#!/bin/bash
TMP="{tmp}"
l=pg_dump; for a in "$@"; do l="$l $a"; done; printf '%s\\n' "$l" >>"$TMP/calls.log"
prev=""
for a in "$@"; do
  [ "$prev" = "-f" ] && printf 'fake-dump\\n' >"$a"
  prev="$a"
done
if [ "${{FAKE_PG_DUMP_FAIL:-}}" = 1 ]; then exit 1; fi
exit 0
"""

PG_RESTORE_FAKE = """\
#!/bin/bash
TMP="{tmp}"
l=pg_restore; for a in "$@"; do l="$l $a"; done; printf '%s\\n' "$l" >>"$TMP/calls.log"
if [ "${{FAKE_PG_RESTORE_FAIL:-}}" = 1 ]; then exit 1; fi
exit 0
"""

PG_ISREADY_FAKE = """\
#!/bin/bash
TMP="{tmp}"
l=pg_isready; for a in "$@"; do l="$l $a"; done; printf '%s\\n' "$l" >>"$TMP/calls.log"
if [ "${{FAKE_NOTREADY:-}}" = 1 ]; then exit 1; fi
exit 0
"""

DEFAULT_TABLES = "t1 t2 t3"


class Drill:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        for name, text in (
            ("docker", DOCKER_FAKE),
            ("psql", PSQL_FAKE),
            ("pg_dump", PG_DUMP_FAKE),
            ("pg_restore", PG_RESTORE_FAKE),
            ("pg_isready", PG_ISREADY_FAKE),
        ):
            f = bin_dir / name
            f.write_text(text.format(tmp=str(tmp_path)))
            os.chmod(f, 0o755)
        self.env = {
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "SRC_DSN": "postgresql://postgres@127.0.0.1:55998/keeper",
            "DRILL_PORT": "55999",
            "DRILL_NAME": "drill-test",
            "DRILL_MEMORY": "1g",
            "DRILL_HEALTH_TIMEOUT": "2",
            "FAKE_TABLES_SRC": DEFAULT_TABLES,
            "FAKE_TABLES_RESTORE": DEFAULT_TABLES,
            "FAKE_COUNTS_SRC": "t1=10 t2=5 t3=7",
            "FAKE_COUNTS_RESTORE": "t1=10 t2=5 t3=7",
        }

    def run(self, timeout=60, extra_env=None):
        env = dict(self.env)
        env.update(extra_env or {})
        return subprocess.run([BASH, str(SCRIPT)], env=env,
                              capture_output=True, text=True, timeout=timeout)

    def lines(self):
        log = self.tmp / "calls.log"
        return log.read_text().splitlines() if log.exists() else []


@pytest.fixture
def drill(tmp_path):
    return Drill(tmp_path)


def test_ok_path_prints_summary_and_per_table_lines(drill):
    r = drill.run()
    assert r.returncode == 0, r.stdout + r.stderr
    lines = r.stdout.splitlines()
    assert lines[:3] == ["table t1 10", "table t2 5", "table t3 7"]
    assert re.fullmatch(r"PG-DRILL ok rto_s=\d+ rows=22", lines[-1]), lines
    assert len(lines) == 4


def test_ok_path_drives_the_expected_commands(drill):
    r = drill.run()
    assert r.returncode == 0, r.stdout + r.stderr
    lines = drill.lines()
    runs = [l for l in lines if l.startswith("docker run")]
    assert len(runs) == 1
    for token in ("--name drill-test", "--memory 1g",
                  "POSTGRES_HOST_AUTH_METHOD=trust", "127.0.0.1:55999:5432", "postgres:16"):
        assert token in runs[0]
    dumps = [l for l in lines if l.startswith("pg_dump")]
    assert len(dumps) == 1 and "-Fc" in dumps[0]
    restores = [l for l in lines if l.startswith("pg_restore")]
    assert len(restores) == 1 and "--no-owner" in restores[0]
    assert "postgresql://postgres@127.0.0.1:55999/keeper_drill" in restores[0]
    # the throwaway container is always removed, including on success
    assert "docker rm -f drill-test" in lines
    joined = "\n".join(lines)
    assert "0.0.0" + ".0" not in joined  # loopback-only


def test_dump_dir_created_by_default_is_removed_on_exit(drill):
    r = drill.run()
    assert r.returncode == 0, r.stdout + r.stderr
    dumps = [l for l in drill.lines() if l.startswith("pg_dump")]
    dump_dir = Path(dumps[0].split("-f ")[1]).parent
    assert not dump_dir.exists()


def test_count_mismatch_fails_naming_the_table(drill):
    r = drill.run(extra_env={"FAKE_COUNTS_RESTORE": "t1=10 t2=4 t3=7"})
    assert r.returncode == 1
    assert any(l.startswith("PG-DRILL fail") and "t2" in l for l in r.stdout.splitlines())
    assert not any(l.startswith("PG-DRILL ok") for l in r.stdout.splitlines())


def test_missing_table_fails_naming_the_table(drill):
    r = drill.run(extra_env={"FAKE_TABLES_RESTORE": "t1 t3"})
    assert r.returncode == 1
    assert any(l.startswith("PG-DRILL fail") and "t2" in l for l in r.stdout.splitlines())


def test_zero_restored_tables_fails_as_vacuous(drill):
    r = drill.run(extra_env={"FAKE_TABLES_RESTORE": ""})
    assert r.returncode == 1
    assert any(l.startswith("PG-DRILL fail vacuous") for l in r.stdout.splitlines())


def test_zero_total_rows_fails_as_vacuous(drill):
    r = drill.run(extra_env={"FAKE_COUNTS_SRC": "t1=0 t2=0 t3=0",
                             "FAKE_COUNTS_RESTORE": "t1=0 t2=0 t3=0"})
    assert r.returncode == 1
    assert any(l.startswith("PG-DRILL fail vacuous") for l in r.stdout.splitlines())
    assert not any(l.startswith("PG-DRILL ok") for l in r.stdout.splitlines())


def test_container_removed_on_failure(drill):
    drill.run(extra_env={"FAKE_COUNTS_RESTORE": "t1=10 t2=4 t3=7"})
    assert any(l.startswith("docker rm") and "drill-test" in l for l in drill.lines())
    assert not (drill.tmp / "container").exists()


def test_notready_container_fails_within_health_budget(drill):
    r = drill.run(timeout=30, extra_env={"FAKE_NOTREADY": "1"})
    assert r.returncode == 1
    assert any(l.startswith("PG-DRILL fail") for l in r.stdout.splitlines())
    assert any(l.startswith("docker rm") and "drill-test" in l for l in drill.lines())


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_shellcheck_clean():
    r = subprocess.run(["shellcheck", str(SCRIPT)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
