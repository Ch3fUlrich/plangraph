import importlib.util, sys, json, os, signal, time
from types import SimpleNamespace
import subprocess
from pathlib import Path
import pytest; from fake_pid import FAKE_PID
ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "e2e_devstack.py"
LIVE_PORTS = (7233, 7243, 8233, 55432, 8787)
DEVSTACK_PORTS = (17252, 18235, 55440, 18787, 18233)
@pytest.fixture(scope="module")
def e2e():
    name = "e2e_devstack"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    assert spec is not None and spec.loader is not None, f"cannot load {SCRIPT}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module            # registered before exec: modules look themselves up by name
    spec.loader.exec_module(module)
    return module
@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
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
    assert str(port) in str(exc.value) and "live/default port" in str(exc.value)
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
    assert "must be loopback" in str(exc.value) and "--temporal" in str(exc.value)
@pytest.mark.parametrize("addr", ["nonsense", "127.0.0.1", "127.0.0.1:", "127.0.0.1:abc"])
def test_hostport_requires_host_and_port(e2e, addr):
    with pytest.raises(e2e.Fail) as exc:
        e2e._hostport(addr, "--temporal")
    assert "must be HOST:PORT" in str(exc.value)
@pytest.mark.parametrize("url", ["http://127.0.0.1:8787", "http://127.0.0.1:8233",
                                 "http://127.0.0.1:7233", "http://127.0.0.1:7243", "http://127.0.0.1:55432"])
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
    assert "--temporal" in str(exc.value) and "7233" in str(exc.value)
def test_missing_http_refuses_before_keeper_write(e2e, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--temporal", "127.0.0.1:27233",
                                     "--keeper", "http://127.0.0.1:18787"])
    monkeypatch.setattr(e2e, "add_principal", lambda *a: pytest.fail("keeper write"))
    monkeypatch.setattr(e2e, "run", lambda *a: pytest.fail("preflight passed"))
    assert e2e.main() == 1 and "PLANGRAPH_TEMPORAL_HTTP is required" in capsys.readouterr().err
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
    assert proc.returncode == 0 and (tmp_path / "out.txt").read_text() == "done\n"
    event = json.loads(proc.stdout)
    assert event["type"] == "text" and event["part"]["text"] == "wrote out.txt"
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
    child = tmp_path / "child"
    code = ("import subprocess,sys; "
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
            "open(sys.argv[1],'w').write(str(p.pid))")
    proc = subprocess.Popen([sys.executable, "-c", code, str(child)], start_new_session=True)
    try:
        with open(os.devnull, "wb") as out:
            os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOWAIT)
            e2e.stop_worker(SimpleNamespace(proc=proc, out=out, owned_pgid=proc.pid))
        pid = int(child.read_text())
        assert_descendant_stopped(pid)
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
    assert "--keeper" in str(exc.value) and "8787" in str(exc.value)
@pytest.mark.parametrize("port", [7243, 8233])
def test_validate_args_rejects_a_live_temporal_http_port(e2e, port):
    args = e2e.parse_args(["--temporal", "127.0.0.1:17252",
                           "--keeper", "http://127.0.0.1:18787",
                           "--temporal-http", f"http://127.0.0.1:{port}"])
    with pytest.raises(e2e.Fail) as exc:
        e2e.validate_args(args)
    assert "--temporal-http" in str(exc.value) and str(port) in str(exc.value)
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
    assert args.temporal == "127.0.0.1:7233" and args.keeper == "http://127.0.0.1:18787"
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
    monkeypatch.setattr(os, "getpgid", lambda pid: pytest.fail("group discovery"))
    monkeypatch.setattr(os, "killpg", lambda *a: pytest.fail("unowned group"))
    proc = SimpleNamespace(pid=FAKE_PID, poll=lambda: None, send_signal=lambda sig: None,
                           wait=lambda **kw: None)
    with open(os.devnull, "wb") as out:
        e2e.stop_worker(SimpleNamespace(proc=proc, out=out))
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
@pytest.mark.parametrize("stop_reason", [None, "run interrupted", "run time box expired"])
@pytest.mark.parametrize("reason", ["workflow_not_merged", "merge_sha_mismatch", None])
def test_teardown_preserves_primary_reason(e2e, capsys, reason, stop_reason):
    import ast
    tree = ast.parse(SCRIPT.read_text())
    final = next(n.finalbody for n in ast.walk(tree) if isinstance(n, ast.Try)
                 and any(isinstance(x, ast.Assign) and any(isinstance(t, ast.Name)
                         and t.id == "teardown_failed" for t in x.targets) for x in n.finalbody))
    def stop():
        raise e2e.Fail(stop_reason) if stop_reason else RuntimeError("withheld")
    body = [ast.If(test=ast.Name(id="reason", ctx=ast.Load()),
                   body=[ast.Raise(exc=ast.Call(func=ast.Name(id="Fail", ctx=ast.Load()),
                         args=[ast.Name(id="reason", ctx=ast.Load())], keywords=[]))], orelse=[])]
    code = ast.fix_missing_locations(ast.Module(body=[ast.Try(body=body, handlers=[],
                                                             orelse=[], finalbody=final)], type_ignores=[]))
    with pytest.raises(e2e.Fail, match=reason or stop_reason or "teardown_failed"):
        exec(compile(code, str(SCRIPT), "exec"), dict(sys=sys, Fail=e2e.Fail, reason=reason,
                                                    cleanup=[("worker", stop)]))
    assert f"teardown worker: {'Fail' if stop_reason else 'RuntimeError'}" in capsys.readouterr().err
@pytest.mark.parametrize("forbidden", ["WSL", "npm shim", "/tmp/opencode"])
def test_driver_has_no_site_specific_assumptions(forbidden):
    assert forbidden not in SCRIPT.read_text()
def test_run_does_not_shadow_signal_module(e2e):
    assert "signal" not in e2e.run.__code__.co_varnames
@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGALRM, signal.SIGINT])
@pytest.mark.parametrize("window", ["constructor", "ownership"])
def test_worker_spawn_signal_is_deferred_until_registered(e2e, sig, window):
    import ast, threading; tree = ast.parse(SCRIPT.read_text())
    run = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "run")
    body = next(n.body for n in run.body if isinstance(n, ast.Try))
    start = next(i for i, n in enumerate(body) if isinstance(n, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id in ("blocked", "worker")
                         for t in n.targets))
    end = next(i for i in range(start, len(body)) if isinstance(body[i], ast.Try)
               and any(isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
                       and isinstance(n.value.func, ast.Attribute)
                       and n.value.func.attr == "set_current_version" for n in body[i].body))
    code = compile(ast.Module(body=body[start:end], type_ignores=[]), str(SCRIPT), "exec")
    cleanup, stopped, events = [], [], []
    class Worker:
        proc = SimpleNamespace(pid=FAKE_PID)
        def __init__(self, *args):
            if window == "constructor":
                os.kill(os.getpid(), sig); time.sleep(0.01)  # allow peer delivery
            events.append("constructed")
        def __setattr__(self, name, value):
            if window == "ownership":
                os.kill(os.getpid(), sig); time.sleep(0.01)
            object.__setattr__(self, name, value)
    def interrupt(signum, frame):
        events.append("interrupted")
        assert cleanup and cleanup[-1][0] == "worker", "worker not registered"
        raise e2e.Fail("spawn interrupted")
    previous = signal.signal(sig, interrupt)
    mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())
    idle = threading.Event(); peer = threading.Thread(target=idle.wait); peer.start()
    try:
        with pytest.raises(e2e.Fail, match="spawn interrupted"):
            try:
                exec(code, dict(signal=signal, u=SimpleNamespace(WorkerProc=Worker),
                                worker_bin=None, env={}, Path=Path, os=os,
                                cleanup=cleanup, stop_worker=stopped.append))
            finally:
                while cleanup:
                    cleanup.pop()[1]()
        assert len(stopped) == 1
        assert stopped[0].owned_pgid == FAKE_PID
        assert events == ["constructed", "interrupted"]
    finally:
        idle.set(); peer.join(timeout=5)
        signal.signal(sig, previous); signal.pthread_sigmask(signal.SIG_SETMASK, mask)
@pytest.mark.parametrize("failure", ["signal", "waitid", "permission"])
def test_stop_worker_grace_failure_still_kills_and_reaps(e2e, tmp_path, monkeypatch, failure):
    child = tmp_path / "child"
    code = ("import os,signal,sys,time; "
            "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
            "pid=os.fork(); "
            "open(sys.argv[1],'w').write(str(pid)) if pid else None; "
            "time.sleep(60)")
    proc = subprocess.Popen([sys.executable, "-c", code, str(child)], start_new_session=True)
    old = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT, signal.SIGALRM)}
    sleep, killpg = time.sleep, os.killpg
    out = open(os.devnull, "wb")
    def grace_sleep(seconds):
        os.kill(os.getpid(), signal.SIGALRM)
    def denied_term(pgid, sig):
        if sig == signal.SIGTERM:
            raise PermissionError("denied")
        killpg(pgid, sig)
    try:
        deadline = time.monotonic() + 5
        while not child.exists() or not child.read_text():
            assert time.monotonic() < deadline, "worker not ready"
            sleep(0.01)
        pid = int(child.read_text())
        with monkeypatch.context() as patch:
            if failure == "signal":
                signal.signal(signal.SIGALRM, e2e.interrupted)
                patch.setattr(time, "sleep", grace_sleep)
            elif failure == "waitid":
                patch.setattr(os, "waitid", lambda *a: (_ for _ in ()).throw(OSError("waitid failed")))
            else:
                patch.setattr(os, "killpg", denied_term)
            worker = SimpleNamespace(proc=proc, out=out, owned_pgid=proc.pid)
            if failure == "permission":
                e2e.stop_worker(worker)
            else:
                with pytest.raises(e2e.Fail if failure == "signal" else OSError):
                    e2e.stop_worker(worker)
        assert proc.returncode is not None and out.closed, "worker not reaped or output not closed"
        assert_descendant_stopped(pid)
    finally:
        for sig, handler in old.items():
            signal.signal(sig, handler)
        try:
            killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=5)
        out.close()
@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT, signal.SIGALRM])
@pytest.mark.parametrize("window", ["kill", "reap"])
def test_final_worker_cleanup_defers_first_signal(e2e, monkeypatch, sig, window):
    import threading  # pthread_sigmask protects this thread, not CI/plugin peers.
    idle = threading.Event(); events = []
    peer = threading.Thread(target=idle.wait); peer.start()  # exercise an unblocked peer
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    killpg, wait, out = os.killpg, proc.wait, open(os.devnull, "wb")
    old = {s: signal.signal(s, e2e.interrupted) for s in (signal.SIGTERM, signal.SIGINT, signal.SIGALRM)}
    def deliver():  # allow peer delivery and main-thread handler execution before proceeding
        signal.pthread_kill(peer.ident, sig); time.sleep(0.02)
    def send(pgid, sent):
        if sent == signal.SIGTERM: return
        if window == "kill": deliver()
        killpg(pgid, sent); events.append("killed")
    def reap(**kwargs):
        if window == "reap": deliver()
        result = wait(**kwargs); events.append("reaped"); return result
    try:
        with monkeypatch.context() as patch:
            patch.setattr(os, "killpg", send); patch.setattr(os, "waitid", lambda *a: True)
            patch.setattr(proc, "wait", reap)
            with pytest.raises(e2e.Fail, match="run time box expired" if sig == signal.SIGALRM else "run interrupted"):
                e2e.stop_worker(SimpleNamespace(proc=proc, out=out, owned_pgid=proc.pid))
        assert events == ["killed", "reaped"] and proc.returncode == -signal.SIGKILL and out.closed
    finally:
        idle.set(); peer.join(timeout=5)
        for s, handler in old.items(): signal.signal(s, handler)
        try: killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError: pass
        wait(timeout=5); out.close()
def assert_descendant_stopped(pid):
    for _ in range(100):
        try:
            if Path(f"/proc/{pid}/stat").read_text().split()[2] == "Z": return
        except (FileNotFoundError, ProcessLookupError): return
        time.sleep(0.01)
    pytest.fail("worker descendant survived teardown")
