"""Hermetic one-shot oversight audit contracts."""
import json
from pathlib import Path

import pytest

from plangraph.keeper import cli_oversight as audit
from plangraph.keeper.cli import build_parser
from plangraph.keeper.oversight import digest


@pytest.fixture
def inputs(tmp_path):
    text, sha = digest([])
    audit.write_digest(tmp_path, text, sha)
    log = tmp_path / "decisions.md"
    log.write_text("| D-1 | HOLD |\n")
    return tmp_path, log, tmp_path / "cursor.json", sha


def run(inputs, keys=(), emit=None):
    directory, log, cursor, _ = inputs
    audit.audit_once(directory, log, cursor, keys, emit=emit)


def test_cap_and_dropped_count(inputs, capsys):
    inputs[1].write_text("".join(f"| D-{i} | GO |\n" for i in range(1, 14)))
    run(inputs)
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 10
    assert lines[-1] == "dropped 5 findings"
    assert inputs[3] in lines[0] and "last D-13" in lines[0]


def test_unchanged_is_one_quiet_line(inputs, capsys):
    run(inputs)
    capsys.readouterr()
    run(inputs)
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1 and "quiet" in lines[0]
    assert inputs[3] in lines[0] and "last D-1" in lines[0]


def test_go_and_unknown_closes(inputs, capsys):
    inputs[1].write_text(
        "| D-2 | fixture | GO Closes: fixture:missing |\n"
        f"| D-3 | fixture | GO GO-HEAD: {'a' * 40} Closes fixture:known |\n"
        f"| D-4 | fixture | GO GO-HEAD: {'a' * 41} |\n")
    run(inputs, lambda: {"fixture:known"})
    output = capsys.readouterr().out
    assert "D-2: GO without 40-hex GO-HEAD" in output
    assert "Closes unknown keeper key fixture:missing" in output
    assert "D-3:" not in output
    assert "D-4: GO without" in output


def test_go_requires_standalone_token(inputs, capsys):
    inputs[1].write_text(
        "| D-1 | NO-GO |\n"
        "| D-2 | GO-HEAD: missing |\n"
        "| D-3 | GO |\n"
        "| D-4 | fixture | GO GO-HEAD: abc123 |\n"
        f"| D-5 | fixture | GO GO-HEAD: {'a' * 40} |\n"
        "| D-6 | XGO GO_X GO-away |\n")
    run(inputs)
    lines = capsys.readouterr().out.splitlines()
    assert lines[1:] == [
        "D-3: GO without 40-hex GO-HEAD",
        "D-4: GO without 40-hex GO-HEAD",
    ]


def test_closes_stops_at_cells_and_directives(inputs, capsys):
    inputs[1].write_text(
        "| D-1 | HOLD | Closes: fixture:known | fixture:other |\n"
        "| D-2 | HOLD | Closes: fixture:known Waives: fixture:waived |\n"
        "| D-3 | HOLD | Closes: fixture:known, fixture:missing "
        "fixture:absent Waives: fixture:waived |\n")
    run(inputs, {"fixture:known"})
    lines = capsys.readouterr().out.splitlines()
    assert lines[1:] == [
        "D-3: Closes unknown keeper key fixture:missing",
        "D-3: Closes unknown keeper key fixture:absent",
    ]


@pytest.mark.parametrize("punctuation", [".", ",", ";", ")", "]"])
def test_closes_strips_trailing_punctuation(inputs, capsys, punctuation):
    inputs[1].write_text(
        f"| D-1 | fixture | Closes: plangraph:ITEM-X{punctuation} |\n"
        f"| D-2 | fixture | Closes: fixture:missing{punctuation} |\n")
    run(inputs, {"plangraph:ITEM-X"})
    assert capsys.readouterr().out.splitlines()[1:] == [
        "D-2: Closes unknown keeper key fixture:missing",
    ]


def test_project_cell_directives_are_ignored(inputs, capsys):
    inputs[1].write_text("| D-1 | GO Closes: x:y | HOLD |\n")
    run(inputs)
    assert "quiet" in capsys.readouterr().out


def test_cursor_only_advances_after_all_prints(inputs):
    run(inputs, emit=lambda _: None)
    before = inputs[2].read_bytes()
    inputs[1].write_text("| D-2 | GO |\n")
    seen = []

    def fail(line):
        assert inputs[2].read_bytes() == before
        seen.append(line)
        if len(seen) == 2:
            raise BrokenPipeError()

    with pytest.raises(BrokenPipeError):
        run(inputs, emit=fail)
    assert inputs[2].read_bytes() == before
    run(inputs, emit=lambda _: None)
    assert json.loads(inputs[2].read_text())["last_row"] == 2


def test_new_digest_and_old_rows(inputs, capsys):
    run(inputs)
    capsys.readouterr()
    text, sha = digest([dict(rule="R1", kind="idle", key="fixture:known",
                             reason="idle", detail="fixture\nsecond", ts="")])
    audit.write_digest(inputs[0], text, sha)
    run(inputs)
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2 and sha in lines[0]
    assert "fixture second" in lines[1]


def test_parser_routes_through_existing_connection():
    args = build_parser().parse_args([
        "oversight-audit", "--cursor", "cursor", "--router-log", "decisions.md"])
    assert args.command == "oversight-check" and args.audit
    assert args.router_log == Path("decisions.md")


def test_parser_requires_router_log():
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["oversight-audit", "--cursor", "cursor"])
    assert exc.value.code == 2


def test_cli_only_reads_keys(inputs, monkeypatch, capsys):
    from types import SimpleNamespace

    statements = []
    conn = SimpleNamespace(
        execute=lambda sql: statements.append(sql) or [("fixture:known",)],
        close=lambda: None)
    monkeypatch.setattr(audit.kdb, "open_db", lambda *a, **kw: conn)
    args = SimpleNamespace(audit=True, out_dir=inputs[0],
                           router_log=inputs[1], cursor=inputs[2])
    session = SimpleNamespace(config=SimpleNamespace(db="fixture", db_options={}))
    assert audit.cmd_oversight_check(args, session) == 0
    assert statements == ["SELECT key FROM items"]
    assert inputs[3] in capsys.readouterr().out
