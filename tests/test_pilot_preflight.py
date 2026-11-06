"""Hermetic pilot safety vectors; no keeper, network or agent processes."""
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import shutil

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("pilot", ROOT / "tools/pilot_preflight.py")
pilot = importlib.util.module_from_spec(SPEC)
if SPEC.loader and Path(SPEC.origin).exists():
    SPEC.loader.exec_module(pilot)
SECRET = "".join(("pilot", "sentinel", "private"))


@pytest.fixture
def env(tmp_path, monkeypatch):
    # Helper executables need an executable mount, independent of --basetemp.
    tmp_path = Path(tempfile.mkdtemp(prefix="pilot-tests-", dir="/tmp"))
    for key in list(os.environ):
        if key.startswith(("PG_", "PLANGRAPH_", "GIT_")):
            monkeypatch.delenv(key)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
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
    yield tmp_path
    shutil.rmtree(tmp_path)


def run(capsys, *args):
    code = pilot.main(list(args))
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err
    assert "Traceback" not in output.out + output.err
    assert not output.err
    assert all(line.startswith(("OK ", "REFUSE ")) for line in output.out.splitlines())
    return code, output.out


@pytest.mark.parametrize("name", pilot.REQUIRED_ENV)
@pytest.mark.parametrize("kind", ("missing", "empty", "malformed"))
def test_env_refusal(env, monkeypatch, capsys, name, kind):
    if kind == "missing":
        monkeypatch.delenv(name)
    else:
        monkeypatch.setenv(name, "" if kind == "empty" else "bad\nvalue")
    code, output = run(capsys, "preflight")
    assert code == 2
    assert f"REFUSE {name} " in output
    assert len(output.splitlines()) == len(pilot.REQUIRED_ENV)


@pytest.mark.parametrize("name,value", [
    ("PLANGRAPH_TEMPORAL_HTTP", "ftp://localhost"),
    ("PG_KEEPER_URL", "http://localhost:invalid"),
    ("PG_TEMPORAL_ADDR", "localhost:0"),
    ("PG_JAIL_BIN", "helper"), ("PG_CARD_ALLOWLIST", "/nonexistent/cards"),
    ("PG_BUILD_ID", "bad build"),
    *[("PG_HARNESS_VERSIONS", v) for v in
      ("helper", "helper=", "helper=one=two", "helper=one,helper=two", "!bad=one")],
])
def test_malformed_values(env, monkeypatch, capsys, name, value):
    monkeypatch.setenv(name, value)
    assert f"REFUSE {name} " in run(capsys, "preflight")[1]


def test_non_utf8_allowlist(env, capsys):
    Path(os.environ["PG_CARD_ALLOWLIST"]).write_bytes(bytes([255]))
    assert "REFUSE PG_CARD_ALLOWLIST" in run(capsys, "preflight")[1]


def test_name_parity():
    sources = [ROOT / "rust/worker/src/bin/worker.rs",
               ROOT / "rust/worker-core/src/config.rs"]
    if not all(p.exists() for p in sources):
        pytest.skip("Rust worker sources absent")
    worker = "\n".join(p.read_text() for p in sources)
    # These names are delegated or pilot-only, not literals in the entrypoint.
    delegated = {
        "PG_JAIL_BIN": "rust/worker/src/harness.rs",
        "PG_BUILD_ID": "rust/worker/src/buildid.rs",
        "PG_CARD_ALLOWLIST": "tools/pilot_preflight.py",
        "PLANGRAPH_TEMPORAL_HTTP": "src/plangraph/inbox/temporal_signal.py",
    }
    for name in pilot.REQUIRED_ENV:
        text = (ROOT / delegated[name]).read_text() if name in delegated else worker
        assert name in text, name


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
    with tempfile.NamedTemporaryFile(dir="/tmp") as db:
        repo = env / "repo"
        assert run(capsys, "setup-repo", str(repo))[0] == 0
        code, output = run(capsys, "verify", "--temporal-db", db.name,
                           "--jail-bin", os.environ["PG_JAIL_BIN"], "--repo", str(repo))
    assert code == 2
    assert "REFUSE persistence" in output
    assert_checks(output)


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


def assert_checks(output):
    assert [line.split()[1] for line in output.splitlines()] == [
        "persistence", "auth", "jail", "pins", "checks"]


@pytest.mark.parametrize("failure", ["persistence", "auth", "jail", "pins", "checks",
                                    "missing-pin", "missing-check", "remote", "helper",
                                    "missing-token", "empty-token", "not-repo"])
def test_verify_failures(env, monkeypatch, capsys, failure):
    repo = env / "repo"
    assert run(capsys, "setup-repo", str(repo))[0] == 0
    monkeypatch.setattr(pilot, "persistent", lambda p: failure != "persistence")
    expected = failure
    if failure == "jail":
        Path(os.environ["PG_JAIL_BIN"]).chmod(0o644)
    elif failure in ("pins", "missing-pin"):
        monkeypatch.setenv("PG_HARNESS_VERSIONS",
                           "helper=wrong" if failure == "pins" else "absentpilothelper=one")
        expected = "pins"
    elif failure in ("checks", "missing-check"):
        if failure == "checks":
            (repo / "check.sh").write_text("#!/bin/sh\nexit 1\n")
        else:
            (repo / "check.sh").unlink()
        expected = "checks"
    elif failure in ("auth", "missing-token", "empty-token"):
        token = env / "token"
        if failure == "auth":
            token.write_text(SECRET)
            token.chmod(0o644)
        monkeypatch.setenv("PG_TOKEN_FILE", "" if failure == "empty-token" else str(token))
        expected = "auth"
    elif failure in ("remote", "helper"):
        args = (["remote", "add", "origin", str(env / "local")] if failure == "remote"
                else ["config", "credential.helper", "false"])
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
        expected = "auth"
    elif failure == "not-repo":
        repo = env
        expected = "auth"
    code, output = run(capsys, "verify", "--temporal-db", str(env / "db"),
                       "--jail-bin", os.environ["PG_JAIL_BIN"], "--repo", str(repo))
    assert code == 2
    assert f"REFUSE {expected} " in output
    assert_checks(output)


@pytest.mark.parametrize("kind", ["symlink", "dotdot", "file", "allow", "dangling-allow"])
def test_setup_refusals(env, capsys, kind):
    repo = env / "isolated"
    if kind == "symlink":
        repo.symlink_to(ROOT, target_is_directory=True)
    elif kind == "dotdot":
        repo = ROOT / "unused" / ".." / "forbidden"
    elif kind == "file":
        repo.touch()
    else:
        allow = env / "isolated.cards.allow"
        if kind == "allow":
            allow.touch()
        else:
            allow.symlink_to(env / "absent")
    assert run(capsys, "setup-repo", str(repo))[0] == 2


@pytest.mark.parametrize("action", ["preflight", "setup-repo", "verify"])
def test_redacted_exceptions(env, monkeypatch, capsys, action):
    def fail(*args):
        raise OSError(SECRET)
    monkeypatch.setattr(pilot, action.replace("-", "_"), fail)
    args = {"preflight": [], "setup-repo": [str(env / "repo")],
            "verify": ["--temporal-db", "db", "--jail-bin", "jail", "--repo", "repo"]}
    assert run(capsys, action, *args[action])[0] == 2


def test_verify_success(env, monkeypatch, capsys):
    monkeypatch.setattr(pilot, "persistent", lambda p: True)
    code, output = verify(env, capsys)
    assert code == 0
    assert_checks(output)
    assert "REFUSE" not in output


@pytest.mark.parametrize("operation", ["persistent", "command", "absolute_file", "pins"])
def test_verify_exception_continues(env, monkeypatch, capsys, operation):
    def fail(*args, **kwargs):
        raise OSError(SECRET)
    monkeypatch.setattr(pilot, operation, fail)
    code, output = verify(env, capsys)
    assert code == 2
    assert_checks(output)
