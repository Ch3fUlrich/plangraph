"""tools/pg-drill-local.sh: hermetic drill coverage with fake pg_dump/docker/psql/pg_restore/pg_isready."""
import os
import re
import shutil
import signal
import subprocess
import time
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
  run)
    if [ "${{2:-}}" = "-d" ]; then
      touch "$TMP/container"; exit 0
    fi
    # run --rm ... : client fallback. Skip flags up to the image, then the next arg is the tool.
    tool=""; past=0
    while [ "$#" -gt 0 ]; do
      a="$1"; shift
      if [ "$past" = 1 ]; then tool="$a"; break; fi
      [ "$a" = "postgres:16" ] && past=1
    done
    if [ -n "$tool" ] && [ -x "$TMP/clientbin/$tool" ]; then
      exec "$TMP/clientbin/$tool" "$@"
    fi
    exit 127 ;;
  rm) rm -f "$TMP/container" ;;
esac
exit 0
"""

PSQL_FAKE = """\
#!/bin/bash
TMP="{tmp}"
l=psql; for a in "$@"; do l="$l $a"; done
dsn=""; query=""; sch=""; tbl=""; prev=""
for a in "$@"; do
  case "$prev" in
    -c) query="$a" ;;
    -v)
      [ "${{a#tbl=}}" != "$a" ] && tbl="${{a#tbl=}}"
      [ "${{a#sch=}}" != "$a" ] && sch="${{a#sch=}}"
      ;;
  esac
  case "$a" in
    postgresql://*) dsn="$a" ;;
  esac
  prev="$a"
done
vsch="$sch"
[ -n "$sch" ] || sch="${{FAKE_SCHEMA:-public}}"
if [ -z "$query" ]; then
  IFS= read -r sql
  # STDIN queries carry the raw SQL text: log it so tests can prove it is never rewritten
  l="$l <<$sql"
else
  sql="$query"
fi
printf '%s\\n' "$l" >>"$TMP/calls.log"
# Real psql does NOT expand :"var" inside -c; it is a bare syntax error there.
case "$query" in
  *':"'*) printf 'ERROR: syntax error at or near ":"\\n' >&2; exit 1 ;;
esac
case "$sql" in
  *"CREATE DATABASE"*) exit 0 ;;
  *information_schema*)
    if [ "$dsn" = postgresql://postgres@127.0.0.1:55998/keeper ]; then var=FAKE_TABLES_SRC; else var=FAKE_TABLES_RESTORE; fi
    tables="${{!var}}"
    old_ifs="$IFS"; IFS=' '
    for t in $tables; do printf '%s\\t%s\\n' "$sch" "$t"; done
    IFS="$old_ifs"
    exit 0 ;;
  *"count(*)"*)
    # a count against a schema that holds no such table is an error, as in real psql
    if [ "$vsch" != "${{FAKE_SCHEMA:-public}}" ]; then
      printf 'ERROR: relation "%s.%s" does not exist\n' "$vsch" "$tbl" >&2; exit 1
    fi
    if [ "$dsn" = postgresql://postgres@127.0.0.1:55998/keeper ]; then var=FAKE_COUNTS_SRC; else var=FAKE_COUNTS_RESTORE; fi
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

CLIENT_TOOLS = ("psql", "pg_dump", "pg_restore", "pg_isready")

COUNT_SQL = 'SELECT count(*) FROM :"sch".:"tbl"'

REFUSAL = ("PG-DRILL refused: non-dev target; a LIVE drill needs a separate GO "
           "and a password-auth drill container")


class Drill:
    def __init__(self, tmp_path, host_tools=True):
        self.tmp = tmp_path
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        clients = (
            ("psql", PSQL_FAKE),
            ("pg_dump", PG_DUMP_FAKE),
            ("pg_restore", PG_RESTORE_FAKE),
            ("pg_isready", PG_ISREADY_FAKE),
        )
        docker_only = [
            ("docker", DOCKER_FAKE),
            *(clients if host_tools else []),
        ]
        for name, text in docker_only:
            f = bin_dir / name
            f.write_text(text.format(tmp=str(tmp_path)))
            os.chmod(f, 0o755)
        if not host_tools:
            client_dir = tmp_path / "clientbin"
            client_dir.mkdir()
            for name, text in clients:
                f = client_dir / name
                f.write_text(text.format(tmp=str(tmp_path)))
                os.chmod(f, 0o755)
        self.env = {
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "SRC_DSN": "postgresql://postgres@127.0.0.1:55998/keeper",
            "PG_PORT": "55998",
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

    def spawn(self, extra_env=None):
        env = dict(self.env)
        env.update(extra_env or {})
        return subprocess.Popen([BASH, str(SCRIPT)], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True)

    def lines(self):
        log = self.tmp / "calls.log"
        return log.read_text().splitlines() if log.exists() else []

    def wait_for_line(self, substr, timeout=15.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if any(substr in l for l in self.lines()):
                return True
            time.sleep(0.05)
        return False


@pytest.fixture
def drill(tmp_path):
    return Drill(tmp_path)


SYSTEM_TOOLS = ("bash", "env", "dirname", "mktemp", "rm", "date", "sleep", "grep", "id", "cat",
                "touch", "chmod", "tr", "sed", "head", "tail", "mkdir", "basename", "printf")


@pytest.fixture
def drill_nohost(tmp_path):
    drill = Drill(tmp_path, host_tools=False)
    # PATH = fake docker + symlinks to only the plain system tools the script needs, so the
    # real pg clients are unreachable even on a host (or CI runner) that has them installed
    sysbin = tmp_path / "sysbin"
    sysbin.mkdir()
    for tool in SYSTEM_TOOLS:
        real = shutil.which(tool)
        if real:
            (sysbin / tool).symlink_to(real)
    drill.env["PATH"] = f"{tmp_path / 'bin'}:{sysbin}"
    for tool in CLIENT_TOOLS:
        assert shutil.which(tool, path=drill.env["PATH"]) is None, tool
    return drill


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
    r = drill.run(extra_env={"FAKE_COUNTS_RESTORE": "t1=10 t2=4 t3=7"})
    assert r.returncode != 0
    assert any(l.startswith("docker rm") and "drill-test" in l for l in drill.lines())
    assert not (drill.tmp / "container").exists()


def test_pg_dump_failure_fails_before_container_starts(drill):
    r = drill.run(extra_env={"FAKE_PG_DUMP_FAIL": "1"})
    assert r.returncode == 1
    assert any(l.startswith("PG-DRILL fail") and "pg_dump" in l for l in r.stdout.splitlines())
    assert not any(l.startswith("PG-DRILL ok") for l in r.stdout.splitlines())
    assert not any(l.startswith("docker run") for l in drill.lines())
    assert not (drill.tmp / "container").exists()


def test_pg_restore_failure_removes_container(drill):
    r = drill.run(extra_env={"FAKE_PG_RESTORE_FAIL": "1"})
    assert r.returncode == 1
    assert any(l.startswith("PG-DRILL fail") and "pg_restore" in l for l in r.stdout.splitlines())
    assert any(l.startswith("docker rm") and "drill-test" in l for l in drill.lines())
    assert not (drill.tmp / "container").exists()


def test_extra_table_only_in_restore_fails(drill):
    r = drill.run(extra_env={"FAKE_TABLES_RESTORE": "t1 t2 t3 t4"})
    out = f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert r.returncode == 1, out
    assert any(l.startswith("PG-DRILL fail") and "t4" in l for l in r.stdout.splitlines()), out
    assert not any(l.startswith("PG-DRILL ok") for l in r.stdout.splitlines()), out


def test_hostile_table_name_reaches_psql_only_via_dash_v(drill):
    hostile = 'a";DROP_TABLE_x;--'
    tables = f'{hostile} t2'
    counts = f'{hostile}=3 t2=5'
    r = drill.run(extra_env={"FAKE_TABLES_SRC": tables, "FAKE_TABLES_RESTORE": tables,
                             "FAKE_COUNTS_SRC": counts, "FAKE_COUNTS_RESTORE": counts})
    assert r.returncode == 0, r.stdout + r.stderr
    assert f'table {hostile} 3' in r.stdout.splitlines()
    sql = COUNT_SQL
    lines = drill.lines()
    count_calls = [l for l in lines if sql in l]
    # the count query is sent on STDIN with the SQL text unchanged: the name is never
    # concatenated into the statement, it travels only as the -v tbl= value
    assert len(count_calls) == 4  # src+restore x 2 tables
    for l in count_calls:
        assert l.endswith(f'<<{sql}')
        assert f'-v tbl={hostile}' in l or f'-v tbl=t2' in l
        assert hostile not in l[l.index('<<'):]
    joined = "\n".join(lines)
    assert f'public."{hostile}"' not in joined
    assert f'{sql}{hostile}' not in joined


def test_sigterm_during_run_removes_container(drill):
    # never-ready container keeps the drill in its health wait, so the signal cannot race a finished run
    proc = drill.spawn(extra_env={"FAKE_NOTREADY": "1", "DRILL_HEALTH_TIMEOUT": "30"})
    try:
        assert drill.wait_for_line("docker run"), "drill never reached docker run"
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=30) == 143
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert any(l.startswith("docker rm") and "drill-test" in l for l in drill.lines())
    assert not (drill.tmp / "container").exists()


def test_notready_container_fails_within_health_budget(drill):
    r = drill.run(timeout=30, extra_env={"FAKE_NOTREADY": "1"})
    assert r.returncode == 1
    assert any(l.startswith("PG-DRILL fail") for l in r.stdout.splitlines())
    assert any(l.startswith("docker rm") and "drill-test" in l for l in drill.lines())


def test_missing_host_tools_fall_back_to_postgres_image(drill_nohost):
    r = drill_nohost.run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert re.fullmatch(r"PG-DRILL ok rto_s=\d+ rows=22", r.stdout.splitlines()[-1]), r.stdout
    lines = drill_nohost.lines()
    runs = [l for l in lines if l.startswith("docker run")]
    assert len([l for l in runs if l.startswith("docker run -d")]) == 1
    rm_runs = [l for l in lines if l.startswith("docker run --rm")]
    for tool in CLIENT_TOOLS:
        assert any(f" postgres:16 {tool}" in l for l in rm_runs), f"no docker fallback for {tool}: {lines}"
    # every fallback call runs as the caller's uid:gid with the dump dir bind-mounted
    # at the identical in-container path, so -f/-d paths pass through unchanged
    dump_runs = [l for l in rm_runs if " postgres:16 pg_dump" in l]
    assert len(dump_runs) == 1, lines
    tokens = dump_runs[0].split()
    dump_file = tokens[tokens.index("-f") + 1]
    mount = f"{Path(dump_file).parent}:{Path(dump_file).parent}"
    for l in rm_runs:
        assert re.search(r"--user \S+:\S+", l), l
        assert f" -v {mount}" in l, l
        assert " --memory 1g " in l, l
    restore_runs = [l for l in rm_runs if " postgres:16 pg_restore" in l]
    assert len(restore_runs) == 1, lines
    assert f"{Path(dump_file).parent}/keeper.dump" in restore_runs[0], restore_runs[0]
    # count queries reach the containerized psql on STDIN, SQL text unchanged
    count_calls = [l for l in lines if COUNT_SQL in l]
    assert len(count_calls) == 6  # src+restore x 3 tables
    for l in count_calls:
        assert l.endswith(f"<<{COUNT_SQL}"), l
        assert "-v sch=public" in l, l
        assert re.search(r"-v tbl=\S+", l), l
    assert "0.0.0" + ".0" not in "\n".join(lines)
    assert not (drill_nohost.tmp / "container").exists()


def test_host_tools_used_unchanged_when_present(drill):
    r = drill.run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert not any(l.startswith("docker run --rm") for l in drill.lines())


def test_hostile_name_also_safe_via_docker_fallback(drill_nohost):
    hostile = 'a";DROP_TABLE_x;--'
    tables = f'{hostile} t2'
    counts = f'{hostile}=3 t2=5'
    r = drill_nohost.run(extra_env={"FAKE_TABLES_SRC": tables, "FAKE_TABLES_RESTORE": tables,
                                    "FAKE_COUNTS_SRC": counts, "FAKE_COUNTS_RESTORE": counts})
    assert r.returncode == 0, r.stdout + r.stderr
    sql = COUNT_SQL
    count_calls = [l for l in drill_nohost.lines() if sql in l]
    assert len(count_calls) == 4  # src+restore x 2 tables
    for l in count_calls:
        assert "-v sch=public" in l, l
        assert f"-v tbl={hostile}" in l or "-v tbl=t2" in l, l
        assert hostile not in l[l.index("<<"):], l
    joined = "\n".join(drill_nohost.lines())
    assert f"{sql}{hostile}" not in joined
    # every logged occurrence of the hostile name is a -v tbl= value, nowhere else
    for l in drill_nohost.lines():
        if hostile in l:
            assert f"-v tbl={hostile}" in l, l


def test_vacuous_failure_message_tells_operator_what_to_do(tmp_path):
    cases = [
        (tmp_path / "rows", {"FAKE_COUNTS_SRC": "t1=0 t2=0 t3=0",
                             "FAKE_COUNTS_RESTORE": "t1=0 t2=0 t3=0"}),
        (tmp_path / "tables", {"FAKE_TABLES_RESTORE": ""}),
    ]
    for sub, env in cases:
        sub.mkdir()
        r = Drill(sub).run(extra_env=env)
        assert r.returncode == 1, (env, r.stdout)
        fails = [l for l in r.stdout.splitlines() if l.startswith("PG-DRILL fail vacuous:")]
        assert fails, (env, r.stdout)
        for hint in ("devstack.sh start", "devstack.sh keeper", "SRC_DSN"):
            assert any(hint in l for l in fails), (env, r.stdout)


def test_non_public_schema_tables_are_counted(drill):
    r = drill.run(extra_env={"FAKE_SCHEMA": "k_abc"})
    assert r.returncode == 0, r.stdout + r.stderr
    lines = r.stdout.splitlines()
    assert lines[:3] == ["table k_abc.t1 10", "table k_abc.t2 5", "table k_abc.t3 7"]
    assert re.fullmatch(r"PG-DRILL ok rto_s=\d+ rows=22", lines[-1]), lines
    counts = [l for l in drill.lines() if COUNT_SQL in l]
    assert len(counts) == 6 and all("-v sch=k_abc" in l for l in counts), counts


def test_non_dev_dsn_refused(drill):
    r = drill.run(extra_env={"SRC_DSN": "postgresql://postgres@db.example.invalid:55998/keeper"})
    assert r.returncode == 2
    assert r.stdout.strip() == REFUSAL
    assert not any(l.startswith(("pg_dump", "docker")) for l in drill.lines())


def test_localhost_dev_dsn_passes(drill):
    r = drill.run(extra_env={"SRC_DSN": "postgresql://postgres@localhost:55998/keeper"})
    assert r.returncode != 2
    assert REFUSAL not in r.stdout
    assert any(l.startswith("pg_dump") for l in drill.lines())


def test_right_host_wrong_port_refused(drill):
    r = drill.run(extra_env={"SRC_DSN": "postgresql://postgres@127.0.0.1:5432/keeper"})
    assert r.returncode == 2
    assert r.stdout.strip() == REFUSAL
    assert not any(l.startswith(("pg_dump", "docker")) for l in drill.lines())


def test_no_port_dsn_refused(drill):
    r = drill.run(extra_env={"SRC_DSN": "postgresql://postgres@127.0.0.1/keeper"})
    assert r.returncode == 2
    assert r.stdout.strip() == REFUSAL
    assert not any(l.startswith(("pg_dump", "docker")) for l in drill.lines())


def test_query_string_host_override_refused(drill):
    r = drill.run(extra_env={"SRC_DSN": "postgresql://postgres@127.0.0.1:55998/keeper?host=db.example.invalid&port=5432"})
    assert r.returncode == 2
    assert r.stdout.strip() == REFUSAL
    assert not any(l.startswith(("pg_dump", "docker")) for l in drill.lines())


def test_hostile_dsn_refused_without_shell_effects(drill):
    r = drill.run(extra_env={"SRC_DSN": "postgresql://postgres@127.0.0.1:55998$(touch PWNED)/keeper"})
    assert r.returncode == 2
    assert not (drill.tmp / "PWNED").exists()


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_shellcheck_clean():
    r = subprocess.run(["shellcheck", str(SCRIPT)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
