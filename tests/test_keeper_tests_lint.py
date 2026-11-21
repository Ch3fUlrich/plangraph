"""Keeper tests use only connection calls both backends have (LE-KEEPER-TESTS-NEVER-RUN-ON-POSTGRES-BEFORE-CI).

PgConnection has execute but no executemany, executescript or cursor: a test that calls them on a
World connection passes on SQLite and errors first in the Postgres CI job."""
import re
import sys
from pathlib import Path

from keeper_util import World

TESTS = Path(__file__).resolve().parent
SQLITE_ONLY = re.compile(r"\b(?:w|world|self\.w|self\.world)\.conn\.(executemany|executescript|cursor)\(")


def offenders(text):
    return [(n, m.group(0)) for n, line in enumerate(text.splitlines(), 1)
            for m in [SQLITE_ONLY.search(line)] if m and not line.lstrip().startswith("#")]


def test_the_lint_flags_every_sqlite_only_connection_call():
    assert offenders("w.conn.executemany('x', rows)\n") == [(1, "w.conn.executemany(")]
    assert offenders("    world.conn.executescript(s)\n") == [(1, "world.conn.executescript(")]
    assert offenders("w.conn.execute('x')\nw.execute_many('x', rows)\n# w.conn.executemany(1)\n") == []


def test_no_keeper_test_uses_a_sqlite_only_connection_call():
    found = {p.name: offenders(p.read_text(encoding="utf-8")) for p in sorted(TESTS.rglob("*.py"))
             if p.name != Path(__file__).name}
    assert {name: hits for name, hits in found.items() if hits} == {}


def test_world_execute_many_runs_once_per_row(tmp_path):
    w = World(tmp_path)
    try:
        w.execute_many("INSERT INTO edges(src,dst,rel) VALUES(?,?,'depends_on')",
                       [("demo:A", "demo:B"), ("demo:A", "demo:C")])
        rows = w.conn.execute("SELECT dst FROM edges WHERE src='demo:A' ORDER BY dst").fetchall()
        assert [r[0] for r in rows] == ["demo:B", "demo:C"]
    finally:
        w.close()
