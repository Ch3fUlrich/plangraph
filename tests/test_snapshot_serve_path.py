"""LE-SNAPSHOT-SERVE-PATH (D-3445): pinned snapshot-serve checkout + authorized_keys check.

Mutations killed: a missing PYTHONPATH dir slipping through, a `.claude/worktrees/`
checkout passing (a session cleanup removes those and pulls break silently), key
material leaking into check output, non-snapshot lines being flagged, the printed
forced command deviating from the installed text, `~` paths not expanded.
Round 2: an unquoted spaced path breaking `sh -c` pulls, a quoted or single-quoted
or `python3 -c` PYTHONPATH slipping through as CLEAN, only the first PYTHONPATH /
first `os.pathsep` entry checked, a relative or `~` entry resolved against the
checker's cwd, `command="` matched in a comment/earlier option or truncated at an
escaped `\"`, `print` emitting a `.claude/worktrees/` checkout.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from plangraph.keeper import snapshot_serve_path as ssp  # noqa: E402

SRC = str(Path(__file__).resolve().parents[1] / "src")

BLOB = ("AAAAC3NzaC1lZDI1NTE5AAAAIFAKEKEYMATERIALDONOTPRINT"
        "000000000000000000000000000000000000000")


def key_line(command, comment="pull@hostA"):
    """A full authorized_keys line around the forced command under test."""
    return f'restrict,{command} ssh-ed25519 {BLOB} {comment}'


def test_check_flags_missing_pypath_dir(tmp_path):
    cmd = ssp.forced_command(tmp_path / "gone-checkout", tmp_path / "keeper.json")
    problems = ssp.check_authorized_keys(key_line(cmd))
    assert len(problems) == 1
    assert "line 1" in problems[0]
    assert "does not exist" in problems[0]
    assert str(tmp_path / "gone-checkout" / "src") in problems[0]


def test_check_flags_claude_worktrees_path(tmp_path):
    checkout = tmp_path / "repo" / ".claude" / "worktrees" / "le-x"
    (checkout / "src").mkdir(parents=True)   # exists, but cleanup may remove it
    cmd = ssp.forced_command(checkout, tmp_path / "keeper.json")
    problems = ssp.check_authorized_keys(key_line(cmd))
    assert len(problems) == 1
    assert "line 1" in problems[0]
    assert ".claude/worktrees" in problems[0]
    assert str(checkout / "src") in problems[0]


def test_pinned_default_dir_passes(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".local/share/plangraph/snapshot-serve/src").mkdir(parents=True)
    cmd = ssp.forced_command(ssp.PINNED_DEFAULT, "~/.local/share/plangraph/keeper.json")
    keys = tmp_path / "authorized_keys"
    keys.write_text(key_line(cmd) + "\n")
    assert ssp.check_authorized_keys(keys.read_text()) == []
    assert ssp.main(["check", "--authorized-keys", str(keys)]) == 0


def test_key_material_never_in_output(tmp_path, capsys):
    keys = tmp_path / "authorized_keys"
    keys.write_text(key_line(ssp.forced_command(tmp_path / "gone", tmp_path / "k.json")) + "\n")
    assert ssp.main(["check", "--authorized-keys", str(keys)]) == 1
    out = capsys.readouterr().out
    assert BLOB not in out
    assert "ssh-ed25519" not in out
    assert "pull@hostA" not in out
    assert "restrict" not in out


def test_non_snapshot_lines_ignored(tmp_path):
    (tmp_path / "src").mkdir()
    text = "\n".join([
        f"ssh-ed25519 {BLOB} plain-key-no-command",
        f'command="/usr/local/bin/deploy" ssh-ed25519 {BLOB} other-command',
        f'command="PYTHONPATH=/definitely/gone /usr/bin/python3 -m plangraph.keeper.cli x" '
        f"ssh-ed25519 {BLOB} other-module",
        f"# plangraph.keeper.snapshot_serve in a comment, no command=",
        "",
    ])
    assert ssp.check_authorized_keys(text) == []


def test_print_matches_expected_string(tmp_path, capsys):
    checkout, config = tmp_path / "co", tmp_path / "keeper.json"
    expected = (f'command="PYTHONPATH={checkout}/src /usr/bin/python3 '
                f"-m plangraph.keeper.snapshot_serve --config {config}\"")
    assert ssp.main(["print", "--checkout", str(checkout), "--config", str(config)]) == 0
    assert capsys.readouterr().out == expected + "\n"
    assert ssp.forced_command(checkout, config) == expected


def test_forced_command_expands_tilde(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert ssp.forced_command("~/snapshot-serve", "~/keeper.json") == (
        f'command="PYTHONPATH={tmp_path}/snapshot-serve/src /usr/bin/python3 '
        f"-m plangraph.keeper.snapshot_serve --config {tmp_path}/keeper.json\"")


def test_line_numbers_across_lines_and_module_cli(tmp_path):
    (tmp_path / "ok/src").mkdir(parents=True)
    good = ssp.forced_command(tmp_path / "ok", tmp_path / "k.json")
    bad = ssp.forced_command(tmp_path / "gone", tmp_path / "k.json")
    keys = tmp_path / "authorized_keys"
    keys.write_text("\n".join([key_line(good), "not-a-key-line", key_line(bad), ""]) + "\n")
    problems = ssp.check_authorized_keys(keys.read_text())
    assert [p.split(":")[0] for p in problems] == ["line 3"]
    env = dict(os.environ, PYTHONPATH=SRC)
    proc = subprocess.run(
        [sys.executable, "-m", "plangraph.keeper.snapshot_serve_path",
         "check", "--authorized-keys", str(keys)],
        capture_output=True, text=True, env=env)
    assert proc.returncode == 1
    assert "line 3" in proc.stdout
    assert BLOB not in proc.stdout + proc.stderr


def test_forced_command_round_trips_through_check(tmp_path):
    """A line forced_command prints must pass check when its dirs exist (spaces too)."""
    checkout = tmp_path / "snap shot" / "serve dir"
    (checkout / "src").mkdir(parents=True)
    config = tmp_path / "keep er.json"
    cmd = ssp.forced_command(checkout, config)
    assert f"PYTHONPATH='{checkout}/src'" in cmd
    assert f"--config '{config}'" in cmd
    keys = tmp_path / "authorized_keys"
    keys.write_text(key_line(cmd) + "\n")
    assert ssp.check_authorized_keys(keys.read_text()) == []
    assert ssp.main(["check", "--authorized-keys", str(keys)]) == 0


def test_spaced_missing_dir_still_flagged(tmp_path):
    gone = tmp_path / "snap shot" / "gone dir" / "src"
    cmd = ssp.forced_command(tmp_path / "snap shot" / "gone dir", tmp_path / "keeper.json")
    problems = ssp.check_authorized_keys(key_line(cmd))
    assert len(problems) == 1
    assert "does not exist" in problems[0]
    assert str(gone) in problems[0]


def test_quoted_pypath_value_parsed(tmp_path):
    src = tmp_path / "a b" / "src"
    src.mkdir(parents=True)
    line = key_line('command="PYTHONPATH=\\"{0}\\" /usr/bin/python3 '
                    '-m plangraph.keeper.snapshot_serve --config /k.json"'.format(src))
    assert ssp.check_authorized_keys(line) == []
    gone = tmp_path / "gone dir" / "src"
    line = key_line("command=\"PYTHONPATH='{0}' /usr/bin/python3 "
                    "-m plangraph.keeper.snapshot_serve --config /k.json\"".format(gone))
    problems = ssp.check_authorized_keys(line)
    assert len(problems) == 1
    assert "does not exist" in problems[0]
    assert str(gone) in problems[0]


def test_unparsable_command_is_problem(tmp_path):
    # unbalanced quote inside the command value: shlex cannot split it
    line = key_line('command="PYTHONPATH=\'/a b/src /usr/bin/python3 '
                    '-m plangraph.keeper.snapshot_serve --config /k.json"')
    problems = ssp.check_authorized_keys(line)
    assert len(problems) == 1
    assert "line 1" in problems[0]
    assert "parse" in problems[0]
    # a python3 -c wrapper: snippet present, but no PYTHONPATH word to check
    line = key_line('command="/usr/bin/python3 -c \'import plangraph.keeper.snapshot_serve\'"')
    problems = ssp.check_authorized_keys(line)
    assert len(problems) == 1
    assert "line 1" in problems[0]
    assert "PYTHONPATH" in problems[0]


def test_single_quoted_command_is_problem(tmp_path):
    line = key_line("command='PYTHONPATH={0} /usr/bin/python3 "
                    "-m plangraph.keeper.snapshot_serve --config /k.json'"
                    .format(tmp_path / "gone" / "src"))
    problems = ssp.check_authorized_keys(line)
    assert len(problems) >= 1
    assert "line 1" in problems[0]
    assert BLOB not in "".join(problems)


def test_multiple_pypath_entries_all_checked(tmp_path):
    a, b, c = (tmp_path / "gone-a" / "src", tmp_path / "gone-b" / "src",
               tmp_path / "gone-c" / "src")
    cmd = ('command="PYTHONPATH={0}:{1} PYTHONPATH={2} /usr/bin/python3 '
           '-m plangraph.keeper.snapshot_serve --config /k.json"').format(a, b, c)
    problems = ssp.check_authorized_keys(key_line(cmd))
    assert len(problems) == 3
    assert [p.split(":")[0] for p in problems] == ["line 1"] * 3
    for path in (a, b, c):
        assert any(str(path) in p for p in problems)


def test_relative_and_tilde_entries_not_absolute():
    cmd = ('command="PYTHONPATH=rel/src:~/x /usr/bin/python3 '
           '-m plangraph.keeper.snapshot_serve --config /k.json"')
    problems = ssp.check_authorized_keys(key_line(cmd))
    assert len(problems) == 2
    assert all("not absolute" in p for p in problems)


def test_worktree_and_missing_two_problems(tmp_path):
    checkout = tmp_path / "repo" / ".claude" / "worktrees" / "le-y"  # src NOT created
    cmd = ssp.forced_command(checkout, tmp_path / "keeper.json")
    problems = ssp.check_authorized_keys(key_line(cmd))
    assert len(problems) == 2
    assert "does not exist" in problems[0]
    assert ".claude/worktrees" in problems[1]


def test_print_refuses_worktree_checkout(tmp_path, capsys):
    checkout = tmp_path / "repo" / ".claude" / "worktrees" / "le-z"
    code = ssp.main(["print", "--checkout", str(checkout), "--config", str(tmp_path / "k.json")])
    assert code == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert ".claude/worktrees" in err


def test_print_refuses_relative_checkout(tmp_path, capsys):
    code = ssp.main(["print", "--checkout", "rel/co", "--config", "/abs/k.json"])
    assert code == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert "relative" in err


def test_command_in_comment_field_ignored(tmp_path):
    text = (f'ssh-ed25519 {BLOB} backup '
            'command="PYTHONPATH=/definitely/gone -m plangraph.keeper.snapshot_serve"')
    assert ssp.check_authorized_keys(text) == []
