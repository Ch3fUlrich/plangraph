"""tools/e2e_devstack.py argument guards: the live/default ports must never be reachable.

Hermetic: the module is loaded by path (it is a standalone script, not an importable package)
and only its pure helpers are exercised -- no Temporal, keeper, network or subprocess runs.
`_refuse_port` / `_hostport` / `_check_url` / `validate_args` are the preflight that keeps an
e2e run off the operator's live 7233/8233/55432/8787, so a regression there is a run that
touches live state.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

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
                           "--leg", "opencode", "--model", "some/model"])
    assert e2e.validate_args(args) == ("http://127.0.0.1:18787", "")


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
