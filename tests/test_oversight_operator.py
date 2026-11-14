"""Hermetic R8 checks; R1–R7 regression lives in test_oversight_checks.py."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from keeper_util import World, defn
from plangraph.keeper import oversight as ov
from plangraph.keeper.events import append_event
from plangraph.keeper.oversight_operator import command_blocks, render


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path, backend="sqlite")
    world.store.create_items(world.p("l1main"), [
        defn("OP", kind="operator_task"), defn("MID", deps=("OP",)),
        defn("TOP", deps=("MID",), kind="goal", priority=9)])
    yield world
    world.close()


def rows(w, text="", hours=0):
    w.conn.execute('UPDATE items SET description=? WHERE key=?', (text, "demo:OP"))
    return ov.r8_operator(w.conn, w.clock() + hours * 3600)


@pytest.mark.parametrize("text,expected", [
    ("```sh\necho ok\n```", True), ("$ echo ok", True), ("! echo ok", True),
    ("$ echo ok\ntested: lab", True), ("$ echo ok\ntested: lab probe ok", False),
    ("plain instructions", False)])
def test_proof(w, text, expected):
    assert bool(rows(w, text)) == expected
    w.conn.execute("UPDATE items SET state='done' WHERE key='demo:OP'")
    assert rows(w, text) == []


@pytest.mark.parametrize("shell", ["sh", "pwsh"])
def test_guard(w, shell):
    text = render("lab", "lab probe ok", "echo ok", shell)
    assert rows(w, text) == []
    assert [r["kind"] for r in rows(w, text.replace("WRONG HOST:", "WRONG:"))] == [
        "operator_step_no_guard"]
    assert rows(w, "host: lab")[0]["kind"] == "operator_step_no_guard"
    assert rows(w, "host: lab\ntested: lab ok\n$ echo ok")[0]["kind"] == "operator_step_no_guard"


@pytest.mark.parametrize("fence", ["```sh", "~~~pwsh", "```"])
def test_fenced_probe_is_not_evidence(w, fence):
    text = fence + "\ncat <<EOF\ntested: lab x\nEOF\n" + fence[:3]
    assert [r["kind"] for r in rows(w, text)] == ["operator_step_untested"]
    assert rows(w, text + "\ntested: lab real probe") == []
    assert rows(w, text.rsplit("\n", 1)[0])[0]["kind"] == "operator_step_untested"


@pytest.mark.parametrize("shell,other", [("sh", "pwsh"), ("pwsh", "sh")])
def test_guard_must_match_fence_shell(w, shell, other):
    text = render("lab", "lab probe ok", "echo ok", shell)
    assert rows(w, text) == []
    for tag in (other, "", "python"):
        changed = text.replace("```" + shell + "\n", "```" + tag + "\n")
        assert [r["kind"] for r in rows(w, changed)] == ["operator_step_no_guard"]


def test_render_shell(tmp_path):
    source = tmp_path / "commands"
    source.write_text("touch marker\n")
    tool = Path(__file__).resolve().parents[1] / "tools/operator_step.py"
    text = subprocess.check_output([sys.executable, str(tool), "render", "--host",
                                   "lab", "--tested", "lab probe ok", str(source)], text=True)
    stub = tmp_path / "hostname"
    stub.write_text('#!/bin/sh\necho "$STUB_HOST"\n')
    stub.chmod(0o755)
    for host in ("other", "lab"):
        result = subprocess.run(["sh", "-i"], input=command_blocks(text)[0] +
                                '\necho SHELL_ALIVE\n', text=True, capture_output=True,
                                cwd=tmp_path, env={**os.environ, "PATH": str(tmp_path) +
                                os.pathsep + os.environ["PATH"], "STUB_HOST": host}, timeout=5)
        assert result.returncode == 0
        assert "SHELL_ALIVE" in result.stdout
        assert ("WRONG HOST: run this on lab" in result.stdout) == (host != "lab")
        assert (tmp_path / "marker").exists() == (host == "lab")
    pwsh = render("lab", "lab ok", "Write-Output ok", "pwsh")
    assert 'if ($env:COMPUTERNAME -cne "lab") {' in pwsh
    assert '} else {\nWrite-Output ok\n}' in pwsh
    with pytest.raises(ValueError):
        render("bad host", "lab ok", "echo ok")


@pytest.mark.parametrize("hours,priority,state,expected", [
    (25, 10, "ready", True), (25, 9, "ready", True), (24, 9, "ready", True), (23, 9, "ready", False),
    (25, 8, "ready", False), (25, 9, "done", False)])
def test_stale(w, hours, priority, state, expected):
    w.conn.execute("UPDATE items SET priority=?, state=? WHERE key='demo:TOP'",
                   (priority, state))
    found = rows(w, hours=hours)
    assert bool(found) == expected
    if expected:
        assert len(found) == 1
        assert found[0]["kind"] == "operator_step_stale"
        assert all(name in found[0]["detail"] for name in ("demo:OP", "demo:TOP", "tester"))
        ov.raise_alarms(w.conn, found, w.clock())
        ov.raise_alarms(w.conn, rows(w, hours=hours), w.clock())
        assert w.conn.execute("SELECT COUNT(*) FROM projector_alarms WHERE cleared_at IS NULL"
                             " AND kind='operator_step_stale'").fetchone()[0] == 1
    w.conn.execute("INSERT INTO edges(src,dst,rel) VALUES('demo:OP','demo:MID','depends_on')")
    assert bool(rows(w, hours=hours)) == expected


def test_failure_and_event(w):
    found = rows(w, hours=25)
    ov.raise_alarms(w.conn, found, w.clock())
    class BrokenRead:
        def execute(self, sql):
            raise OSError("snapshot unavailable")
    failed = ov.r8_operator(BrokenRead(), w.clock())
    assert any(r["rule"] == "R8" and r["kind"] == "oversight_check_failed" for r in failed)
    ov.raise_alarms(w.conn, failed, w.clock())
    assert w.conn.execute("SELECT COUNT(*) FROM projector_alarms WHERE cleared_at IS NULL"
                         " AND kind='operator_step_stale'").fetchone()[0] == 1


def test_activity_and_open_paths(w):
    append_event(w.conn, w.store._iso(w.clock() + 2 * 3600),
                 "router", "router", None, "demo:OP", "heartbeat", {})
    assert rows(w, hours=25) == []
    assert rows(w, hours=26)[0]["kind"] == "operator_step_stale"
    w.conn.execute("UPDATE items SET state='done' WHERE key='demo:MID'")
    assert rows(w, hours=26) == []


def test_aggregate(w):
    text = "$ echo ok"
    direct = rows(w, text)
    assert [r for r in ov.run_checks(w.conn, w.clock()) if r["rule"] == "R8"] == direct
    assert ov.digest(direct) == ov.digest(rows(w, text))
