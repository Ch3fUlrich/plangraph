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
Round 3: a symlinked pin into `.claude/worktrees/` passing lexically, an advisory
(never-compared) PINNED_DEFAULT, `snapshot_serve` reached by file path escaping
inspection, empty PYTHONPATH entries dropped silently, and the missing
authorized_keys rc-2 path.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from plangraph.keeper import snapshot_serve_path as ssp  # noqa: E402

SRC = str(Path(__file__).resolve().parents[1] / "src")

BLOB = "AAAA" + "FAKEKEY" * 10


def key_line(command, comment="pull@hostA"):
    """A full authorized_keys line around the forced command under test."""
    return f'restrict,{command} ssh-ed25519 {BLOB} {comment}'


def test_check_flags_missing_pypath_dir(tmp_path):
    cmd = ssp.forced_command(tmp_path / "gone-checkout", tmp_path / "keeper.json")
    problems = ssp.check_authorized_keys(key_line(cmd), pinned=tmp_path / "gone-checkout")
    assert len(problems) == 1
    assert "line 1" in problems[0]
    assert "does not exist" in problems[0]
    assert str(tmp_path / "gone-checkout" / "src") in problems[0]


def test_check_flags_claude_worktrees_path(tmp_path):
    checkout = tmp_path / "repo" / ".claude" / "worktrees" / "le-x"
    (checkout / "src").mkdir(parents=True)   # exists, but cleanup may remove it
    cmd = ssp.forced_command(checkout, tmp_path / "keeper.json")
    problems = ssp.check_authorized_keys(key_line(cmd), pinned=checkout)
    worktrees = [p for p in problems if ".claude/worktrees" in p]
    assert len(worktrees) == 1
    assert "line 1" in worktrees[0]
    assert str(checkout / "src") in worktrees[0]


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
    assert ssp.main(["check", "--authorized-keys", str(keys),
                     "--pinned", str(tmp_path / "gone")]) == 1
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
    assert ssp.main(["print", "--checkout", str(checkout), "--config", str(config),
                     "--pinned", str(checkout)]) == 0
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
    problems = ssp.check_authorized_keys(keys.read_text(), pinned=tmp_path / "ok")
    assert problems
    assert all(p.startswith("line 3:") for p in problems)
    assert any("does not exist" in p for p in problems)
    env = dict(os.environ, PYTHONPATH=SRC)
    proc = subprocess.run(
        [sys.executable, "-m", "plangraph.keeper.snapshot_serve_path",
         "check", "--authorized-keys", str(keys), "--pinned", str(tmp_path / "ok")],
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
    assert ssp.check_authorized_keys(keys.read_text(), pinned=checkout) == []
    assert ssp.main(["check", "--authorized-keys", str(keys),
                     "--pinned", str(checkout)]) == 0


def test_spaced_missing_dir_still_flagged(tmp_path):
    gone = tmp_path / "snap shot" / "gone dir" / "src"
    cmd = ssp.forced_command(tmp_path / "snap shot" / "gone dir", tmp_path / "keeper.json")
    problems = ssp.check_authorized_keys(key_line(cmd),
                                         pinned=tmp_path / "snap shot" / "gone dir")
    assert len(problems) == 1
    assert "does not exist" in problems[0]
    assert str(gone) in problems[0]


def test_quoted_pypath_value_parsed(tmp_path):
    src = tmp_path / "a b" / "src"
    src.mkdir(parents=True)
    line = key_line('command="PYTHONPATH=\\"{0}\\" /usr/bin/python3 '
                    '-m plangraph.keeper.snapshot_serve --config /k.json"'.format(src))
    assert ssp.check_authorized_keys(line, pinned=tmp_path / "a b") == []
    gone = tmp_path / "gone dir" / "src"
    line = key_line("command=\"PYTHONPATH='{0}' /usr/bin/python3 "
                    "-m plangraph.keeper.snapshot_serve --config /k.json\"".format(gone))
    problems = ssp.check_authorized_keys(line, pinned=tmp_path / "gone dir")
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
    problems = ssp.check_authorized_keys(key_line(cmd), pinned=tmp_path / "gone-a")
    missing = [p for p in problems if "does not exist" in p]
    assert len(missing) == 3
    for path in (a, b, c):
        assert any(str(path) in p for p in missing)
    assert all(p.startswith("line 1:") for p in problems)


def test_relative_and_tilde_entries_not_absolute():
    cmd = ('command="PYTHONPATH=rel/src:~/x /usr/bin/python3 '
           '-m plangraph.keeper.snapshot_serve --config /k.json"')
    problems = ssp.check_authorized_keys(key_line(cmd))
    not_abs = [p for p in problems if "not absolute" in p]
    assert len(not_abs) == 2
    assert any("rel/src" in p for p in not_abs)
    assert any("~/x" in p for p in not_abs)


def test_worktree_and_missing_two_problems(tmp_path):
    checkout = tmp_path / "repo" / ".claude" / "worktrees" / "le-y"  # src NOT created
    cmd = ssp.forced_command(checkout, tmp_path / "keeper.json")
    problems = ssp.check_authorized_keys(key_line(cmd), pinned=checkout)
    assert len(problems) == 2
    assert "does not exist" in problems[0]
    assert ".claude/worktrees" in problems[1]


def test_print_refuses_worktree_checkout(tmp_path, capsys):
    checkout = tmp_path / "repo" / ".claude" / "worktrees" / "le-z"
    code = ssp.main(["print", "--checkout", str(checkout), "--config", str(tmp_path / "k.json"),
                     "--pinned", str(checkout)])
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


def test_symlink_pin_into_worktrees_flagged(tmp_path):
    """A pin that is a SYMLINK into .claude/worktrees/ must still be refused."""
    real = tmp_path / "repo" / ".claude" / "worktrees" / "x"
    (real / "src").mkdir(parents=True)
    pin = tmp_path / "pin-checkout"
    pin.symlink_to(real)          # lexical path looks like the pin
    cmd = ssp.forced_command(pin, tmp_path / "k.json")
    problems = ssp.check_authorized_keys(key_line(cmd), pinned=pin)
    assert len(problems) == 1
    assert ".claude/worktrees" in problems[0]
    assert str(pin / "src") in problems[0]


def test_print_refuses_symlink_into_worktrees(tmp_path, capsys):
    real = tmp_path / "repo" / ".claude" / "worktrees" / "y"
    (real / "src").mkdir(parents=True)
    pin = tmp_path / "pin-checkout"
    pin.symlink_to(real)
    code = ssp.main(["print", "--checkout", str(pin), "--config", "/k.json",
                     "--pinned", str(pin)])
    assert code == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert ".claude/worktrees" in err


def test_dotdot_entry_is_not_the_pin(tmp_path):
    checkout = tmp_path / "co"
    (checkout / "src").mkdir(parents=True)
    entry = str(checkout / "src" / "..")
    cmd = ('command="PYTHONPATH={0} /usr/bin/python3 '
           '-m plangraph.keeper.snapshot_serve --config /k.json"').format(entry)
    problems = ssp.check_authorized_keys(key_line(cmd), pinned=checkout)
    assert len(problems) == 1
    assert "is not the pinned checkout" in problems[0]
    assert entry in problems[0]


def test_empty_pypath_entry_flagged(tmp_path):
    checkout = tmp_path / "co"
    (checkout / "src").mkdir(parents=True)
    for value in (f"{checkout}/src:", f":{checkout}/src"):
        cmd = ('command="PYTHONPATH={0} /usr/bin/python3 '
               '-m plangraph.keeper.snapshot_serve --config /k.json"').format(value)
        problems = ssp.check_authorized_keys(key_line(cmd), pinned=checkout)
        assert len(problems) == 1, problems
        assert "line 1" in problems[0]
        assert "empty entry" in problems[0]


def test_file_path_spelling_with_worktrees_pypath(tmp_path):
    """A file-path `.../snapshot_serve.py` command must be inspected, not skipped."""
    wt = tmp_path / "w" / ".claude" / "worktrees" / "x"
    script = wt / "src" / "plangraph" / "keeper" / "snapshot_serve.py"
    script.parent.mkdir(parents=True)
    script.write_text("")
    cmd = ('command="PYTHONPATH={0}/src /usr/bin/python3 {1} '
           '--config /k.json"').format(wt, script)
    problems = ssp.check_authorized_keys(key_line(cmd), pinned=wt)
    assert len(problems) == 1
    assert ".claude/worktrees" in problems[0]


def test_pin_mismatch_check_and_print(tmp_path, capsys):
    pin = tmp_path / "the-pin"
    (pin / "src").mkdir(parents=True)
    other = tmp_path / "other-co"
    (other / "src").mkdir(parents=True)
    cmd = ssp.forced_command(other, tmp_path / "k.json")
    problems = ssp.check_authorized_keys(key_line(cmd), pinned=pin)
    assert len(problems) == 1
    assert "is not the pinned checkout" in problems[0]
    assert str(other / "src") in problems[0]
    assert f"{pin}/src" in problems[0]
    code = ssp.main(["print", "--checkout", str(other), "--config", "/k.json",
                     "--pinned", str(pin)])
    assert code == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert "pinned checkout" in err


def test_pin_match_passes(tmp_path):
    pin = tmp_path / "the-pin"
    (pin / "src").mkdir(parents=True)
    cmd = ssp.forced_command(pin, tmp_path / "k.json")
    assert ssp.check_authorized_keys(key_line(cmd), pinned=pin) == []
    keys = tmp_path / "authorized_keys"
    keys.write_text(key_line(cmd) + "\n")
    assert ssp.main(["check", "--authorized-keys", str(keys),
                     "--pinned", str(pin)]) == 0


def test_missing_authorized_keys_file_rc2(tmp_path, capsys):
    missing = tmp_path / "no-such-authorized_keys"
    code = ssp.main(["check", "--authorized-keys", str(missing)])
    assert code == 2
    err = capsys.readouterr().err
    assert str(missing) in err


def test_module_cli_round_trip(tmp_path):
    pin = tmp_path / "the-pin"
    (pin / "src").mkdir(parents=True)
    env = dict(os.environ, PYTHONPATH=SRC)
    base = [sys.executable, "-m", "plangraph.keeper.snapshot_serve_path"]
    proc = subprocess.run(
        base + ["print", "--checkout", str(pin), "--config", str(tmp_path / "k.json"),
                "--pinned", str(pin)],
        capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr
    keys = tmp_path / "authorized_keys"
    keys.write_text(key_line(proc.stdout.strip()) + "\n")
    proc = subprocess.run(
        base + ["check", "--authorized-keys", str(keys), "--pinned", str(pin)],
        capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
