"""tools/e2e_devstack.py argument guards: the live/default ports must never be reachable.

Hermetic: the module is loaded by path (it is a standalone script, not an importable package)
and only its pure helpers are exercised -- no Temporal, keeper, network or subprocess runs.
`_refuse_port` / `_hostport` / `_check_url` / `validate_args` are the preflight that keeps an
e2e run off the operator's live 7233/8233/55432/8787, so a regression there is a run that
touches live state.
"""
import importlib.util
import sys
import json
import os
import signal
import subprocess
from pathlib import Path

import pytest
from fake_pid import FAKE_PID

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "e2e_devstack.py"

#: the ports the script must refuse; the module's own REFUSED_PORTS is asserted against these.
LIVE_PORTS = (7233, 8233, 55432, 8787)
#: devstack ports that must pass.
DEVSTACK_PORTS = (17252, 18235, 55440, 18787, 18233)


@pytest.fixture(scope="module")
def e2e():
    """The script as a module, loaded by path (it is not on any import path)."""
    name = "e2e_devstack"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    assert spec is not None and spec.loader is not None, f"cannot load {SCRIPT}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module            # registered before exec: modules look themselves up by name
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """The arg parser falls back to these env vars; clear them so a case tests only its argv."""
    for var in ("TEMPORAL_ADDR", "KEEPER_URL", "PLANGRAPH_TEMPORAL_HTTP"):
        monkeypatch.delenv(var, raising=False)


def test_refused_ports_are_the_live_defaults(e2e):
    assert e2e.REFUSED_PORTS == frozenset(LIVE_PORTS)


def test_standalone_help_preserves_driver_description():
    result = subprocess.run([sys.executable, str(SCRIPT), "--help"],
                            capture_output=True, text=True, cwd=ROOT.parent)
    assert result.returncode == 0, result.stderr
    assert "RT-P1 end-to-end run against an already-running devstack" in result.stdout


@pytest.mark.parametrize("field, value", [
    ("dsn", "postgresql://postgres@127.0.0.1:55432/keeper"),
    ("bind", "127.0.0.1:8787"),
])
def test_keeper_config_refuses_live_ports_after_split(e2e, tmp_path, field, value):
    cfg = tmp_path / "keeper.json"
    cfg.write_text(json.dumps({"dsn": "postgresql://postgres@127.0.0.1:55440/keeper",
                               "bind": "127.0.0.1:18787", field: value}))
    with pytest.raises(e2e.Fail, match="live/default port"):
        e2e.check_keeper_config(cfg)


@pytest.mark.parametrize("coerce", [int, str], ids=["int", "str"])
@pytest.mark.parametrize("port", LIVE_PORTS)
def test_refuse_port_rejects_each_live_port(e2e, port, coerce):
    with pytest.raises(e2e.Fail) as exc:
        e2e._refuse_port(coerce(port), "--temporal")
    assert str(port) in str(exc.value)
    assert "live/default port" in str(exc.value)


@pytest.mark.parametrize("coerce", [int, str], ids=["int", "str"])
@pytest.mark.parametrize("port", DEVSTACK_PORTS)
def test_refuse_port_accepts_devstack_ports(e2e, port, coerce):
    assert e2e._refuse_port(coerce(port), "--temporal") is None


def test_hostport_rejects_a_live_port(e2e):
    with pytest.raises(e2e.Fail) as exc:
        e2e._hostport("127.0.0.1:7233", "--temporal")
    assert "must not use the live/default port 7233" in str(exc.value)


def test_hostport_returns_the_devstack_host_and_port(e2e):
    assert e2e._hostport("127.0.0.1:17252", "--temporal") == ("127.0.0.1", "17252")


@pytest.mark.parametrize("addr", ["example.invalid:17252", "localhost:17252"])
def test_hostport_rejects_non_loopback(e2e, addr):
    with pytest.raises(e2e.Fail) as exc:
        e2e._hostport(addr, "--temporal")
    assert "must be loopback" in str(exc.value)
    assert "--temporal" in str(exc.value)


@pytest.mark.parametrize("addr", ["nonsense", "127.0.0.1", "127.0.0.1:", "127.0.0.1:abc"])
def test_hostport_requires_host_and_port(e2e, addr):
    with pytest.raises(e2e.Fail) as exc:
        e2e._hostport(addr, "--temporal")
    assert "must be HOST:PORT" in str(exc.value)


@pytest.mark.parametrize("url", ["http://127.0.0.1:8787", "http://127.0.0.1:8233",
                                 "http://127.0.0.1:7233", "http://127.0.0.1:55432"])
def test_check_url_refuses_each_live_port(e2e, url):
    with pytest.raises(e2e.Fail) as exc:
        e2e._check_url(url, "--keeper")
    assert "live/default port" in str(exc.value)


@pytest.mark.parametrize("url", ["http://example.invalid:17252", "http://localhost:17252"])
def test_check_url_refuses_non_loopback(e2e, url):
    with pytest.raises(e2e.Fail) as exc:
        e2e._check_url(url, "--keeper")
    assert "must be loopback" in str(exc.value)


@pytest.mark.parametrize("url", ["http://127.0.0.1", "http://127.0.0.1/"])
def test_check_url_refuses_a_url_without_an_explicit_port(e2e, url):
    with pytest.raises(e2e.Fail) as exc:
        e2e._check_url(url, "--keeper")
    assert "explicit port" in str(exc.value)


def test_check_url_accepts_a_loopback_devstack_port(e2e):
    assert e2e._check_url("http://127.0.0.1:18787", "--keeper") == "http://127.0.0.1:18787"


def test_validate_args_without_temporal_or_env_mentions_the_flag(e2e):
    args = e2e.parse_args(["--keeper", "http://127.0.0.1:18787"])
    assert args.temporal == ""
    with pytest.raises(e2e.Fail) as exc:
        e2e.validate_args(args)
    assert "--temporal" in str(exc.value)


def test_validate_args_rejects_a_live_temporal_port(e2e):
    args = e2e.parse_args(["--temporal", "127.0.0.1:7233",
                           "--keeper", "http://127.0.0.1:18787"])
    with pytest.raises(e2e.Fail) as exc:
        e2e.validate_args(args)
    assert "--temporal" in str(exc.value)
    assert "7233" in str(exc.value)


def test_missing_http_refuses_before_keeper_write(e2e, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--temporal", "127.0.0.1:27233",
                                     "--keeper", "http://127.0.0.1:18787"])
    monkeypatch.setattr(e2e, "add_principal", lambda *a: pytest.fail("keeper write"))
    monkeypatch.setattr(e2e, "run", lambda *a: pytest.fail("preflight passed"))
    assert e2e.main() == 1
    assert "PLANGRAPH_TEMPORAL_HTTP is required" in capsys.readouterr().err


def test_box_default_and_validation(e2e):
    assert e2e.parse_args([]).box_seconds == 1800
    args = e2e.parse_args(["--temporal", "127.0.0.1:27233", "--keeper",
                           "http://127.0.0.1:18787", "--temporal-http",
                           "http://127.0.0.1:27243", "--box-seconds", "0"])
    with pytest.raises(e2e.Fail, match="must be positive"):
        e2e.validate_args(args)


def test_stub_resume_emits_recognizable_text(e2e, tmp_path):
    e2e.write_stub_opencode(tmp_path / "bin")
    proc = subprocess.run([str(tmp_path / "bin/opencode"), "run", "-m", "some/model",
                           e2e.u.ANSWER_TOKEN], cwd=tmp_path, capture_output=True, text=True)
    assert proc.returncode == 0
    assert (tmp_path / "out.txt").read_text() == "done\n"
    event = json.loads(proc.stdout)
    assert event["type"] == "text"
    assert event["part"]["text"] == "wrote out.txt"


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGALRM])
def test_signal_cleanup_reaps_only_owned_worker(e2e, tmp_path, sig):
    from types import SimpleNamespace
    owned = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    old = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT, signal.SIGALRM)}
    try:
        with pytest.raises(e2e.Fail):
            try:
                e2e.interrupted(sig, None)
            finally:
                with open(tmp_path / "log", "wb") as out:
                    e2e.stop_worker(SimpleNamespace(proc=owned, out=out))
        assert owned.returncode is not None
        assert other.poll() is None
    finally:
        for s, handler in old.items():
            signal.signal(s, handler)
        for proc in (owned, other):
            if proc.poll() is None:
                proc.kill()
            proc.wait()


def test_url_refusal_does_not_echo_credentials(e2e):
    with pytest.raises(e2e.Fail) as exc:
        e2e._check_url("http://private:do-not-print@127.0.0.1:18787", "--keeper")
    assert "do-not-print" not in str(exc.value)


def test_define_token_is_memory_only(e2e, tmp_path, monkeypatch):
    def run(cmd, what, **kwargs):
        fd, = kwargs["pass_fds"]
        assert os.read(fd, 100) == b"test-token"
        assert kwargs["env"]["PG_TOKEN_FILE"] == f"/proc/self/fd/{fd}"
        assert not (tmp_path / "router.token").exists()
    monkeypatch.setattr(e2e, "_run", run)
    e2e.define_item(tmp_path / "config", "http://127.0.0.1:18787",
                    "test-token", "test", tmp_path)
    assert all("test-token" not in p.read_text() for p in tmp_path.iterdir())


def test_stop_worker_kills_group_after_parent_exit(e2e, tmp_path):
    from types import SimpleNamespace
    import time
    child = tmp_path / "child"
    code = ("import subprocess,sys; "
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
            "open(sys.argv[1],'w').write(str(p.pid))")
    proc = subprocess.Popen([sys.executable, "-c", code, str(child)], start_new_session=True)
    try:
        with open(os.devnull, "wb") as out:
            # Observe exit without reaping: retain the leader's pid through teardown.
            os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOWAIT)
            e2e.stop_worker(SimpleNamespace(proc=proc, out=out, owned_pgid=proc.pid))
        pid = int(child.read_text())
        for _ in range(100):
            status = Path(f"/proc/{pid}/stat")
            if not status.exists() or status.read_text().split()[2] == "Z":
                break
            time.sleep(0.01)
        else:
            pytest.fail("worker descendant survived teardown")
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_validate_args_rejects_a_live_keeper_port(e2e):
    args = e2e.parse_args(["--temporal", "127.0.0.1:17252",
                           "--keeper", "http://127.0.0.1:8787"])
    with pytest.raises(e2e.Fail) as exc:
        e2e.validate_args(args)
    assert "--keeper" in str(exc.value)
    assert "8787" in str(exc.value)


def test_validate_args_rejects_a_live_temporal_http_port(e2e):
    args = e2e.parse_args(["--temporal", "127.0.0.1:17252",
                           "--keeper", "http://127.0.0.1:18787",
                           "--temporal-http", "http://127.0.0.1:8233"])
    with pytest.raises(e2e.Fail) as exc:
        e2e.validate_args(args)
    assert "--temporal-http" in str(exc.value)
    assert "8233" in str(exc.value)


def test_validate_args_opencode_leg_needs_a_model(e2e):
    args = e2e.parse_args(["--temporal", "127.0.0.1:17252",
                           "--keeper", "http://127.0.0.1:18787", "--leg", "opencode"])
    with pytest.raises(e2e.Fail) as exc:
        e2e.validate_args(args)
    assert "--model" in str(exc.value)


def test_validate_args_returns_keeper_and_http_for_a_valid_set(e2e):
    args = e2e.parse_args(["--temporal", "127.0.0.1:17252",
                           "--keeper", "http://127.0.0.1:18787",
                           "--temporal-http", "http://127.0.0.1:18233", "--leg", "stub"])
    assert e2e.validate_args(args) == ("http://127.0.0.1:18787", "http://127.0.0.1:18233")


def test_validate_args_accepts_the_opencode_leg_with_a_model(e2e):
    args = e2e.parse_args(["--temporal", "127.0.0.1:17252",
                           "--keeper", "http://127.0.0.1:18787",
                            "--temporal-http", "http://127.0.0.1:27243",
                            "--leg", "opencode", "--model", "some/model"])
    assert e2e.validate_args(args) == ("http://127.0.0.1:18787", "http://127.0.0.1:27243")


def test_leg_stub_fail_is_an_accepted_choice_and_anything_else_exits(e2e):
    args = e2e.parse_args(["--temporal", "127.0.0.1:17252",
                           "--keeper", "http://127.0.0.1:18787", "--leg", "stub-fail"])
    assert args.leg == "stub-fail"
    with pytest.raises(SystemExit) as exc:
        e2e.parse_args(["--leg", "bogus"])
    assert exc.value.code == 2


def test_env_temporal_addr_is_used_and_also_refused(e2e, monkeypatch):
    monkeypatch.setenv("TEMPORAL_ADDR", "127.0.0.1:7233")
    monkeypatch.setenv("KEEPER_URL", "http://127.0.0.1:18787")
    args = e2e.parse_args([])
    assert args.temporal == "127.0.0.1:7233"
    assert args.keeper == "http://127.0.0.1:18787"
    with pytest.raises(e2e.Fail) as exc:
        e2e.validate_args(args)
    assert "7233" in str(exc.value)


@pytest.mark.parametrize("change", ["keeper", "port", "http", "db", "server", "missing", "none"])
def test_state_binds_endpoints(e2e, tmp_path, monkeypatch, change):
    args = e2e.parse_args(["--state", str(tmp_path), "--temporal", "127.0.0.1:17252",
                          "--keeper", "http://127.0.0.1:18787", "--temporal-http",
                          "http://127.0.0.1:18233"])
    (tmp_path / "keeper.json").write_text(json.dumps({"bind": "127.0.0.1:18787"}))
    (tmp_path / "temporal.pid").write_text("123")
    cmd = ["temporal", "server", "start-dev", "--ip", "127.0.0.1", "--port", "17252",
           "--http-port", "18233", "--db-filename", str(tmp_path / "temporal.db")]
    if change == "keeper":
        args.keeper = "http://127.0.0.1:18788"
    for case, flag in [("port", "--port"), ("http", "--http-port"), ("db", "--db-filename")]:
        if change == case:
            cmd[cmd.index(flag) + 1] = "foreign"
    if change == "server":
        cmd[2] = "other"
    def read_bytes(path):
        if change == "missing":
            raise FileNotFoundError
        return "\0".join(cmd).encode()
    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    if change == "none":
        e2e.check_devstack(args, args.keeper, args.temporal_http)
    else:
        with pytest.raises(e2e.Fail, match="state_"):
            e2e.check_devstack(args, args.keeper, args.temporal_http)


@pytest.mark.parametrize("dsn", ["postgresql://postgres@192.0.2.1:55440/keeper",
                                 "postgresql://postgres@127.0.0.1/keeper"])
def test_keeper_config_refuses_foreign_or_implicit_dsn(e2e, tmp_path, dsn):
    cfg = tmp_path / "keeper.json"
    cfg.write_text(json.dumps({"dsn": dsn, "bind": "127.0.0.1:18787"}))
    with pytest.raises(e2e.Fail, match="loopback|explicit port"):
        e2e.check_keeper_config(cfg)


def test_stop_worker_does_not_discover_or_guess_group(e2e, monkeypatch):
    from types import SimpleNamespace
    support = sys.modules["e2e_devstack_support"]
    monkeypatch.setattr(os, "getpgid", lambda pid: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(os, "killpg", lambda *a: pytest.fail("unowned group"))
    proc = SimpleNamespace(pid=FAKE_PID, poll=lambda: None, send_signal=lambda sig: None,
                           wait=lambda **kw: None)
    with open(os.devnull, "wb") as out:
        support.stop_worker(SimpleNamespace(proc=proc, out=out))


def test_foreign_keeper_refused_before_any_contact(e2e, tmp_path, monkeypatch):
    args = e2e.parse_args(["--state", str(tmp_path), "--temporal", "127.0.0.1:17252",
                          "--keeper", "http://127.0.0.1:18787", "--temporal-http",
                          "http://127.0.0.1:18233", "--worker-bin", str(SCRIPT),
                          "--temporal-cli", str(SCRIPT)])
    (tmp_path / "keeper.json").write_text(json.dumps({"bind": "127.0.0.1:18788",
        "dsn": "postgresql://postgres@127.0.0.1:55440/keeper"}))
    monkeypatch.setattr(e2e.u, "call", lambda *a, **kw: pytest.fail("foreign service contacted"))
    with pytest.raises(e2e.Fail, match="keeper_state_mismatch"):
        e2e.run(args)


@pytest.mark.parametrize("reason", ["workflow_not_merged", "merge_sha_mismatch", None])
def test_teardown_preserves_primary_reason(e2e, capsys, reason):
    import ast
    tree = ast.parse(SCRIPT.read_text())
    final = next(n.finalbody for n in ast.walk(tree) if isinstance(n, ast.Try)
                 and any(isinstance(x, ast.Assign) and any(isinstance(t, ast.Name)
                         and t.id == "teardown_failed" for t in x.targets) for x in n.finalbody))
    def stop():
        raise RuntimeError("withheld")
    body = [ast.If(test=ast.Name(id="reason", ctx=ast.Load()),
                   body=[ast.Raise(exc=ast.Call(func=ast.Name(id="Fail", ctx=ast.Load()),
                         args=[ast.Name(id="reason", ctx=ast.Load())], keywords=[]))], orelse=[])]
    code = ast.fix_missing_locations(ast.Module(body=[ast.Try(body=body, handlers=[],
                                                             orelse=[], finalbody=final)], type_ignores=[]))
    with pytest.raises(e2e.Fail, match=reason or "teardown_failed"):
        exec(compile(code, str(SCRIPT), "exec"), dict(sys=sys, Fail=e2e.Fail, reason=reason,
                                                    cleanup=[("worker", stop)]))
    assert "teardown worker: RuntimeError" in capsys.readouterr().err


@pytest.mark.parametrize("forbidden", ["WSL", "npm shim", "/tmp/opencode"])
def test_driver_has_no_site_specific_assumptions(forbidden):
    assert forbidden not in SCRIPT.read_text()
