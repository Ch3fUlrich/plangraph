"""Hermetic pilot safety vectors; no keeper, network or agent processes."""
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import shutil
import sys
import pytest
ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("pilot", ROOT / "tools/pilot_preflight.py")
pilot = importlib.util.module_from_spec(SPEC)
if SPEC.loader and Path(SPEC.origin).exists():
    SPEC.loader.exec_module(pilot)
SECRET = "".join(("pilot", "sentinel", "private"))
@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("PREFLIGHT_TOOLS_DIR", raising=False)
    tmp_path = Path(tempfile.mkdtemp(prefix="pilot-tests-", dir="/tmp"))
    for key in list(os.environ):
        if key.startswith(("PG_", "PLANGRAPH_", "GIT_")):
            monkeypatch.delenv(key)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
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
    shutil.rmtree(tmp_path, ignore_errors=True)
def run(capsys, *args):
    code = pilot.main(list(args))
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err
    assert "Traceback" not in output.out + output.err
    assert not output.err
    assert all(line.startswith(("OK ", "REFUSE ", "SKIP ")) for line in output.out.splitlines())
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
    code, output = run(capsys, "preflight")
    assert code == 2
    assert f"REFUSE {name} " in output
def test_non_utf8_allowlist(env, capsys):
    Path(os.environ["PG_CARD_ALLOWLIST"]).write_bytes(bytes([255]))
    code, output = run(capsys, "preflight")
    assert code == 2
    assert "REFUSE PG_CARD_ALLOWLIST" in output
def test_name_parity():
    from test_pilot_preflight_findings import assert_name_parity
    assert_name_parity()
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
    monkeypatch.setattr(pilot, "persistent", lambda p: True)
    token = env / "token"
    token.write_text(SECRET)
    token.chmod(0o644)
    monkeypatch.setenv("PG_TOKEN_FILE", str(token))
    assert "REFUSE auth" in verify(env, capsys)[1]
def test_pin_drift(env, monkeypatch, capsys):
    monkeypatch.setattr(pilot, "persistent", lambda p: True)
    monkeypatch.setenv("PG_HARNESS_VERSIONS", "helper=2.0")
    assert "REFUSE pins" in verify(env, capsys)[1]
def test_secret_absent_every_subcommand(env, monkeypatch, capsys):
    monkeypatch.setattr(pilot, "persistent", lambda p: True)
    assert run(capsys, "preflight")[0] == 0
    assert verify(env, capsys)[0] == 0
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
        (env / 'sibling').mkdir()
        repo = env / 'sibling' / '..' / 'forbidden'
    elif kind == "file":
        repo.touch()
    else:
        allow = env / "isolated.cards.allow"
        if kind == "allow":
            allow.touch()
        else:
            allow.symlink_to(env / "absent")
    assert run(capsys, "setup-repo", str(repo))[0] == 2
    if kind == 'dotdot':
        assert not (env / 'forbidden').exists()
        assert not (env / 'forbidden.cards.allow').exists()
@pytest.mark.parametrize("error", [OSError, RuntimeError])
@pytest.mark.parametrize("action", ["preflight", "setup-repo", "verify"])
def test_redacted_exceptions(env, monkeypatch, capsys, action, error):
    def fail(*args):
        raise error(SECRET)
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
@pytest.mark.parametrize("error", [OSError, RuntimeError])
def test_verify_exception_continues(env, monkeypatch, capsys, operation, error):
    calls = []
    def fail(*args, **kwargs):
        calls.append(True)
        raise error(SECRET)
    assert run(capsys, "setup-repo", str(env / "repo"))[0] == 0
    (env / "db").touch()
    monkeypatch.setattr(pilot, "persistent", lambda p: True)
    monkeypatch.setattr(pilot, operation, fail)
    code, output = run(capsys, "verify", "--temporal-db", str(env / "db"),
                       "--jail-bin", os.environ["PG_JAIL_BIN"], "--repo", str(env / "repo"))
    assert code == 2
    assert calls == [True]
    expected = {"persistent": "persistence", "command": "auth",
                "absolute_file": "auth", "pins": "pins"}[operation]
    assert f"REFUSE {expected} " in output
    assert f"SKIP checks not run: {expected} failed" in output
    assert_checks(output)

@pytest.mark.parametrize('text', ['', '# comment\n', 'production:OTHER\n',
                                      'pilot:PILOT-x\n', 'pilot:PILOT-1\n' * 21])
def test_allowlist_content(env, monkeypatch, capsys, text):
    Path(os.environ['PG_CARD_ALLOWLIST']).write_text(text)
    assert run(capsys, 'preflight')[0] == 2
    monkeypatch.setattr(pilot, 'persistent', lambda p: True)
    assert verify(env, capsys)[0] == 2

@pytest.mark.parametrize('credential', ['empty-file', 'empty-env'])
def test_empty_credentials_stop_checks(env, monkeypatch, capsys, credential):
    repo = env / 'repo'
    assert run(capsys, 'setup-repo', str(repo))[0] == 0
    marker = env / 'executed'
    (repo / 'check.sh').write_text(f'#!/bin/sh\n: > "{marker}"\n')
    if credential == 'empty-file':
        token = env / 'token'
        token.write_text('')
        token.chmod(0o600)
        monkeypatch.setenv('PG_TOKEN_FILE', str(token))
    else:
        monkeypatch.setenv('PG_KEEPER_TOKEN', '')
    monkeypatch.setattr(pilot, 'persistent', lambda p: True)
    code, output = run(capsys, 'verify', '--temporal-db', str(env / 'db'),
                       '--jail-bin', os.environ['PG_JAIL_BIN'], '--repo', str(repo))
    assert code == 2
    assert not marker.exists()
    assert 'SKIP checks not run: auth failed' in output

def test_children_sanitized(env, monkeypatch, capsys):
    repo = env / 'repo'
    assert run(capsys, 'setup-repo', str(repo))[0] == 0
    real_home = os.environ['HOME']
    token = env / 'token'
    token.write_text('fixture-credential')
    token.chmod(0o600)
    monkeypatch.setenv('PG_TOKEN_FILE', str(token))
    for name in ('helper', 'check.sh'):
        target = env / name if name == 'helper' else repo / name
        target.write_text('#!/bin/sh\n[ -d "$HOME" ] && [ -w "$HOME" ] || exit 1\n'
                          ': > "$HOME/writable" || exit 1\n' +
                          f'/usr/bin/env > {env / (name + ".env")}\n' +
                          ("echo 'helper 1.0'\n" if name == 'helper' else 'exit 0\n'))
    monkeypatch.setattr(pilot, 'persistent', lambda p: True)
    assert run(capsys, 'verify', '--temporal-db', str(env / 'db'), '--jail-bin',
               os.environ['PG_JAIL_BIN'], '--repo', str(repo))[0] == 0
    homes = []
    for name in ('helper', 'check.sh'):
        values = dict(line.split('=', 1) for line in
                      (env / (name + '.env')).read_text().splitlines())
        names = set(values)
        assert not names & {'PG_KEEPER_TOKEN', 'PG_TOKEN_FILE'}
        assert names <= {'PATH', 'LANG', 'HOME', 'TERM', 'PWD', '_', 'SHLVL'}
        assert values['HOME'] != real_home
        assert not Path(values['HOME']).exists()
        homes.append(values['HOME'])
    assert len(set(homes)) == 2

@pytest.mark.parametrize('helper', [False, True])
def test_auth_real_home(env, monkeypatch, capsys, helper):
    home = Path(os.environ['HOME'])
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(home / '.gitconfig'))
    if helper:
        (home / '.gitconfig').write_text(
            '[credential "https://github.com"]\n\thelper = false\n')
    original = pilot.command
    observed = []
    def command(argv, *args, **kwargs):
        if argv[0] == 'git':
            result = original(['/usr/bin/env'], *args, **kwargs)
            values = dict(line.split('=', 1) for line in result.stdout.decode().splitlines())
            observed.append(values)
        return original(argv, *args, **kwargs)
    # Setup remains isolated; observe only the verification Git calls.
    repo = env / 'repo'
    assert run(capsys, 'setup-repo', str(repo))[0] == 0
    monkeypatch.setattr(pilot, 'command', command)
    monkeypatch.setattr(pilot, 'persistent', lambda p: True)
    code, output = run(capsys, 'verify', '--temporal-db', str(env / 'db'),
                       '--jail-bin', os.environ['PG_JAIL_BIN'], '--repo', str(repo))
    assert code == (2 if helper else 0)
    assert ('REFUSE auth' if helper else 'OK auth') in output
    assert len(observed) == 3
    for values in observed:
        assert values['HOME'] == str(home)
        assert values['GIT_CONFIG_NOSYSTEM'] == '1'
        assert values['GIT_CONFIG_GLOBAL'] == str(home / '.gitconfig')
        assert set(values) <= {'PATH', 'LANG', 'HOME', 'TERM', 'PWD',
                               'GIT_CONFIG_NOSYSTEM', 'GIT_CONFIG_GLOBAL'}

@pytest.mark.parametrize('source', ['global', 'system', 'count', 'xdg'])
@pytest.mark.parametrize('setting', ['credential.helper', 'remote.origin.url'])
def test_auth_config_sources(env, monkeypatch, capsys, source, setting):
    repo = env / 'repo'
    assert run(capsys, 'setup-repo', str(repo))[0] == 0
    config = env / 'auth.config'
    subprocess.run(['git', 'config', '--file', str(config), setting, 'false'], check=True)
    if source == 'count':
        for key, value in {'COUNT': '1', 'KEY_0': setting, 'VALUE_0': 'false'}.items():
            monkeypatch.setenv('GIT_CONFIG_' + key, value)
    elif source == 'xdg':
        monkeypatch.delenv('GIT_CONFIG_GLOBAL')
        xdg = env / 'xdg'
        (xdg / 'git').mkdir(parents=True)
        config.rename(xdg / 'git/config')
        monkeypatch.setenv('XDG_CONFIG_HOME', str(xdg))
    else:
        monkeypatch.setenv('GIT_CONFIG_' + source.upper(), str(config))
        if source == 'system':
            monkeypatch.delenv('GIT_CONFIG_NOSYSTEM')
    monkeypatch.setattr(pilot, 'persistent', lambda p: True)
    code, output = run(capsys, 'verify', '--temporal-db', str(env / 'db'),
                       '--jail-bin', os.environ['PG_JAIL_BIN'], '--repo', str(repo))
    assert code == 2
    assert 'REFUSE auth' in output

@pytest.mark.parametrize('mutation', ['remove', 'chmod', 'symlink'])
def test_child_home_cleanup(env, mutation):
    target = env
    (target / 'marker').touch()
    code = 'import os; home = os.environ["HOME"]; print(home, flush=True); '
    code += ('os.chmod(home, 0o555); ' if mutation == 'chmod' else 'os.rmdir(home); ')
    if mutation == 'symlink':
        code += f'os.symlink({str(target)!r}, home); '
    result = pilot.command([sys.executable, '-c', code + 'exit(7)'])
    assert result.returncode == 7
    assert not os.path.lexists(os.fsdecode(result.stdout).strip())
    assert (target / 'marker').exists()

@pytest.mark.parametrize('fs,expected', [('overlay', False), ('9p', False),
    ('fuse.sshfs', False), ('tmpfs', False), ('ramfs', False), ('nfs', False), ('ext4', True)])
def test_mount_types(tmp_path, monkeypatch, fs, expected):
    db = tmp_path / 'db'
    db.touch()
    # Model /tmp elsewhere: the mount parser must not depend on pytest's location.
    original = Path.resolve
    monkeypatch.setattr(Path, 'resolve', lambda p, *a, **kw:
                        tmp_path / 'other-tmp' if p == Path('/tmp') else original(p, *a, **kw))
    text = f'1 0 8:1 / {tmp_path} rw - {fs} /dev/example rw\n'
    assert pilot.persistent(db, mountinfo=text) is expected

def test_resolved_tmp_refused(tmp_path, monkeypatch):
    db = tmp_path / 'db'
    db.touch()
    original = Path.resolve
    monkeypatch.setattr(Path, 'resolve', lambda p, *a, **kw:
                        tmp_path if p == Path('/tmp') else original(p, *a, **kw))
    assert not pilot.persistent(db, mountinfo='1 0 8:1 / / rw - ext4 device rw\n')

@pytest.mark.parametrize('kind', ['valid', 'missing', 'mode', 'empty', 'malformed'])
def test_file_only_credentials(env, monkeypatch, capsys, kind):
    token = env / 'token'
    if kind != 'missing':
        token.write_text('' if kind == 'empty' else 'bad value' if kind == 'malformed' else SECRET)
        token.chmod(0o644 if kind == 'mode' else 0o600)
    monkeypatch.delenv('PG_KEEPER_TOKEN')
    monkeypatch.setenv('PG_TOKEN_FILE', str(token))
    monkeypatch.setattr(pilot, 'persistent', lambda p: True)
    expected = 0 if kind == 'valid' else 2
    assert run(capsys, 'preflight')[0] == expected
    assert verify(env, capsys)[0] == expected

def test_operator_bound_allowlist(env, monkeypatch, capsys):
    other = env / 'other.cards.allow'
    other.write_text('pilot:PILOT-1\n')
    monkeypatch.setenv('PG_CARD_ALLOWLIST', str(other))
    monkeypatch.setattr(pilot, 'persistent', lambda p: True)
    code, output = verify(env, capsys)
    assert code == 0
    assert 'OK auth' in output
