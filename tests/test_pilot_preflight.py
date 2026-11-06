"""Hermetic pilot safety vectors; no keeper, network or agent processes."""
import importlib.util
import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("pilot", ROOT / "tools/pilot_preflight.py")
pilot = importlib.util.module_from_spec(SPEC)
if SPEC.loader and Path(SPEC.origin).exists():
    SPEC.loader.exec_module(pilot)
SECRET = "pilotsentinel83921"


@pytest.fixture
def env(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith(("PG_", "PLANGRAPH_", "GIT_")):
            monkeypatch.delenv(key)
    helper = tmp_path / "helper"
    helper.write_text("#!/bin/sh\necho 'helper 1.0'\n")
    helper.chmod(0o755)
    allow = tmp_path / "cards.allow"
    allow.write_text("pilot:PILOT-1\npilot:PILOT-2\n")
    values = dict(PLANGRAPH_TEMPORAL_HTTP="http://localhost:8233",
                  PG_KEEPER_URL="https://localhost", PG_KEEPER_TOKEN=SECRET,
                  PG_JAIL_BIN=str(helper), PG_HARNESS_VERSIONS="helper=1.0",
                  PG_BUILD_ID="pilot-build", PG_CARD_ALLOWLIST=str(allow),
                  PG_TEMPORAL_ADDR="localhost:7233")
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    return tmp_path


def run(capsys, *args):
    code = pilot.main(list(args))
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err
    assert all(line.startswith(("OK ", "REFUSE ")) for line in output.out.splitlines())
    return code, output.out


def test_missing_temporal(env, monkeypatch, capsys):
    monkeypatch.delenv("PLANGRAPH_TEMPORAL_HTTP")
    assert run(capsys, "preflight")[0] == 2


def test_empty_temporal(env, monkeypatch, capsys):
    monkeypatch.setenv("PLANGRAPH_TEMPORAL_HTTP", "")
    assert run(capsys, "preflight")[0] == 2


def test_non_http(env, monkeypatch, capsys):
    monkeypatch.setenv("PLANGRAPH_TEMPORAL_HTTP", "ftp://localhost")
    assert run(capsys, "preflight")[0] == 2


def test_empty_token(env, monkeypatch, capsys):
    monkeypatch.setenv("PG_KEEPER_TOKEN", "")
    assert run(capsys, "preflight")[0] == 2


def test_relative_jail(env, monkeypatch, capsys):
    monkeypatch.setenv("PG_JAIL_BIN", "helper")
    assert run(capsys, "preflight")[0] == 2


def test_bad_pins(env, monkeypatch, capsys):
    monkeypatch.setenv("PG_HARNESS_VERSIONS", "helper")
    assert run(capsys, "preflight")[0] == 2


def test_missing_allowlist(env, monkeypatch, capsys):
    monkeypatch.setenv("PG_CARD_ALLOWLIST", str(env / "missing"))
    assert run(capsys, "preflight")[0] == 2


def test_nonempty_repo(env, capsys):
    assert run(capsys, "setup-repo", str(env))[0] == 2


def test_inside_checkout(env, capsys):
    assert run(capsys, "setup-repo", str(ROOT / "pilot-forbidden"))[0] == 2
    assert not (ROOT / "pilot-forbidden").exists()


def test_no_remote_push_fails(env, capsys):
    repo = env / "repo"
    assert run(capsys, "setup-repo", str(repo))[0] == 0
    assert subprocess.check_output(["git", "-C", str(repo), "remote"]) == b""
    assert subprocess.run(["git", "-C", str(repo), "push"], capture_output=True).returncode != 0
    assert (env / "repo.cards.allow").read_text().splitlines() == ["pilot:PILOT-1", "pilot:PILOT-2"]
    assert subprocess.check_output(["git", "-C", str(repo), "ls-files"]).splitlines() == [b"check.sh"]


def verify(env, capsys):
    repo = env / "repo"
    assert run(capsys, "setup-repo", str(repo))[0] == 0
    db = env / "temporal.db"
    db.touch()
    return run(capsys, "verify", "--temporal-db", str(db), "--jail-bin",
               os.environ["PG_JAIL_BIN"], "--repo", str(repo))


def test_tmp_db_refused(env, capsys):
    code, output = verify(env, capsys)
    assert code == 2
    assert "REFUSE persistence" in output


def test_token_mode_refused(env, monkeypatch, capsys):
    token = env / "token"
    token.write_text(SECRET)
    token.chmod(0o644)
    monkeypatch.setenv("PG_TOKEN_FILE", str(token))
    assert "REFUSE auth" in verify(env, capsys)[1]


def test_pin_drift(env, monkeypatch, capsys):
    monkeypatch.setenv("PG_HARNESS_VERSIONS", "helper=2.0")
    assert "REFUSE pins" in verify(env, capsys)[1]


def test_secret_absent_every_subcommand(env, capsys):
    assert run(capsys, "preflight")[0] == 0
    verify(env, capsys)
