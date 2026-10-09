"""PG-LOCAL-COPY (D-818): forced-command snapshot_serve + ``pg sync pull``.

Mutations killed: serving an unsigned/tampered snapshot, honouring the client's
command line, accepting an unsigned/foreign-key/extra-field envelope, installing an
older head_seq or a fork at the same head_seq, a crash between installs letting an
older copy back in, a host/user value becoming an ssh option, concurrent pulls,
``~`` paths in a config not expanded.
"""
from __future__ import annotations

import base64
import fcntl
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("cryptography")

from plangraph.keeper import cli, config as config_mod, snapshot_io, snapshot_pull  # noqa: E402
from plangraph.keeper import snapshot_serve, snapshot_sign  # noqa: E402

SRC = str(Path(__file__).resolve().parents[1] / "src")


def _json(path, data):
    path.write_text(json.dumps(data))
    os.chmod(path, 0o600)
    return path


@pytest.fixture
def keeper(tmp_path):
    """A keeper host dir: key pair, keeper.json, and a writer for signed snapshots."""
    k = tmp_path / "keeper"
    k.mkdir()
    priv, pub = k / "snap.key", k / "snap.pub"
    snapshot_sign.keygen(priv, pub)
    cfg = _json(k / "keeper.json", {"snapshot": str(k / "plan.snapshot.json"),
                                    "signing_key": str(priv), "snapshot_pubkey": str(pub)})

    def write(seq, head=None, items=("p:A",)):
        server = SimpleNamespace(
            store=SimpleNamespace(snapshot=lambda p: {
                "head": head or f"h{seq}", "head_seq": seq,
                "items": [{"key": i} for i in items]}),
            config=SimpleNamespace(snapshot=str(k / "plan.snapshot.json"), signing_key=str(priv)))
        snapshot_io.write_snapshot(server, None)
    return SimpleNamespace(dir=k, cfg=cfg, priv=priv, pub=pub, write=write)


@pytest.fixture
def reader(tmp_path, keeper):
    r = tmp_path / "reader"
    r.mkdir()
    ident = r / "id"
    ident.write_text("x")
    cfg = _json(r / "reader.json", {"snapshot": str(r / "copy" / "plan.snapshot.json"),
                                    "snapshot_pubkey": str(keeper.pub),
                                    "pull": {"host": "keeper.example.test", "user": "s",
                                             "identity": str(ident)}})
    return SimpleNamespace(dir=r, cfg=cfg, snap=r / "copy" / "plan.snapshot.json")


def _serve(keeper):
    return lambda argv: snapshot_serve.envelope(config_mod.load_config(keeper.cfg))


def _pull(reader, fetch):
    return snapshot_pull.pull(config_mod.load_config(reader.cfg), fetch=fetch)


# ----------------------------------------------------------------- serve side
def test_serve_emits_only_the_signed_envelope(keeper, capsys, monkeypatch):
    keeper.write(5)
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "cat /etc/passwd; sh")
    assert snapshot_serve.main(["--config", str(keeper.cfg)]) == 0
    out = capsys.readouterr().out
    assert out.count("\n") == 1
    env = json.loads(out)
    assert set(env) == {"format", "snapshot", "sig"}
    body = base64.b64decode(env["snapshot"])
    assert body == (keeper.dir / "plan.snapshot.json").read_bytes()
    assert snapshot_sign.verify(body, env["sig"], keeper.pub)


def test_serve_refuses_unsigned_tampered_and_unconfigured(keeper, tmp_path, capsys):
    keeper.write(5)
    snap = keeper.dir / "plan.snapshot.json"
    snap.write_bytes(snap.read_bytes().replace(b"p:A", b"p:Z"))
    assert snapshot_serve.main(["--config", str(keeper.cfg)]) == 1
    keeper.write(6)
    snapshot_sign.sig_path(snap).unlink()
    assert snapshot_serve.main(["--config", str(keeper.cfg)]) == 1
    plain = _json(tmp_path / "plain.json", {"snapshot": str(snap)})   # no signing configured
    assert snapshot_serve.main(["--config", str(plain)]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and str(keeper.dir) not in captured.err


def test_serve_refuses_symlinked_snapshot(keeper, tmp_path):
    keeper.write(5)
    snap = keeper.dir / "plan.snapshot.json"
    real = tmp_path / "elsewhere.json"
    snap.rename(real)
    snap.symlink_to(real)
    assert snapshot_serve.main(["--config", str(keeper.cfg)]) == 1


def test_serve_rejects_extra_arguments(keeper):
    with pytest.raises(SystemExit):
        snapshot_serve.main(["--config", str(keeper.cfg), "--snapshot", "/etc/shadow"])


# ------------------------------------------------------------------ pull side
def test_pull_installs_a_verified_copy_and_reader_accepts_it(keeper, reader):
    keeper.write(5)
    assert "head_seq 5" in _pull(reader, _serve(keeper))
    assert snapshot_io.read_snapshot(reader.snap, str(keeper.pub))["head_seq"] == 5
    for p in (reader.snap, snapshot_sign.sig_path(reader.snap), snapshot_pull.hwm_path(reader.snap)):
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
    keeper.write(7)
    assert "(was 5)" in _pull(reader, _serve(keeper))
    assert "(unchanged)" in _pull(reader, _serve(keeper))


def test_pull_refuses_rollback_and_fork(keeper, reader):
    keeper.write(9)
    _pull(reader, _serve(keeper))
    before = reader.snap.read_bytes()
    keeper.write(8)                      # an older, validly signed snapshot
    with pytest.raises(snapshot_pull.PullError, match="older"):
        _pull(reader, _serve(keeper))
    keeper.write(9, head="other")        # same seq, different head
    with pytest.raises(snapshot_pull.PullError, match="different head"):
        _pull(reader, _serve(keeper))
    assert reader.snap.read_bytes() == before


def test_rollback_refused_even_after_a_crash_mid_install(keeper, reader, monkeypatch):
    keeper.write(4)
    _pull(reader, _serve(keeper))
    keeper.write(6)
    real = snapshot_pull._install
    calls = []

    def crash(path, data):
        calls.append(path)
        if len(calls) == 3:              # high-water mark + sig written, snapshot not
            raise OSError("power cut")
        real(path, data)
    monkeypatch.setattr(snapshot_pull, "_install", crash)
    with pytest.raises(OSError):
        _pull(reader, _serve(keeper))
    monkeypatch.setattr(snapshot_pull, "_install", real)
    with pytest.raises(snapshot_sign.SignError):          # the half copy fails closed
        snapshot_io.read_snapshot(reader.snap, str(keeper.pub), _retry=False)
    keeper.write(5)                      # older than the high-water mark
    with pytest.raises(snapshot_pull.PullError, match="older"):
        _pull(reader, _serve(keeper))
    keeper.write(6)
    _pull(reader, _serve(keeper))
    assert snapshot_io.read_snapshot(reader.snap, str(keeper.pub))["head_seq"] == 6


@pytest.mark.parametrize("mutate", ["unsigned", "tamper", "foreign", "extra", "format", "notjson",
                                    "noseq", "boolseq"])
def test_pull_refuses_bad_envelopes(keeper, reader, tmp_path, mutate):
    keeper.write(3)
    env = json.loads(_serve(keeper)(None))
    body = base64.b64decode(env["snapshot"])
    if mutate == "unsigned":
        env["sig"] = ""
    elif mutate == "tamper":
        env["snapshot"] = base64.b64encode(body.replace(b"p:A", b"p:B")).decode()
    elif mutate == "foreign":
        snapshot_sign.keygen(tmp_path / "o.key", tmp_path / "o.pub")
        env["sig"] = snapshot_sign.sign(body, tmp_path / "o.key")
    elif mutate == "extra":
        env["extra"] = "x"
    elif mutate == "format":
        env["format"] = "v0"
    elif mutate in ("noseq", "boolseq"):
        snap = json.loads(body)
        if mutate == "noseq":
            del snap["head_seq"]
        else:
            snap["head_seq"] = True
        body = json.dumps(snap).encode()
        env["snapshot"] = base64.b64encode(body).decode()
        env["sig"] = snapshot_sign.sign(body, keeper.priv)
    data = b"not json" if mutate == "notjson" else json.dumps(env).encode()
    with pytest.raises(snapshot_pull.PullError):
        _pull(reader, lambda argv: data)
    assert not reader.snap.exists()


def test_pull_needs_a_pubkey(keeper, reader):
    cfg = json.loads(reader.cfg.read_text())
    del cfg["snapshot_pubkey"]
    _json(reader.cfg, cfg)
    with pytest.raises(snapshot_pull.PullError, match="snapshot_pubkey"):
        _pull(reader, _serve(keeper))


def test_corrupt_high_water_mark_fails_closed(keeper, reader):
    keeper.write(3)
    _pull(reader, _serve(keeper))
    snapshot_pull.hwm_path(reader.snap).write_text("garbage")
    with pytest.raises(snapshot_pull.PullError, match="high-water"):
        _pull(reader, _serve(keeper))


def test_concurrent_pull_is_refused(keeper, reader):
    keeper.write(3)
    reader.snap.parent.mkdir(parents=True)
    fd = os.open(f"{reader.snap}.lock", os.O_WRONLY | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        with pytest.raises(snapshot_pull.PullError, match="another pull"):
            _pull(reader, _serve(keeper))
    finally:
        os.close(fd)


@pytest.mark.parametrize("field,value", [
    ("host", "-oProxyCommand=sh"), ("host", "a b"), ("host", "h;id"), ("user", "-l"),
    ("user", "root@x"), ("port", "22"), ("port", True), ("port", 0), ("identity", ""),
])
def test_ssh_argv_rejects_option_injection(field, value):
    pull = {"host": "keeper.example.test", "user": "s", "identity": "/k", "port": 22}
    pull[field] = value
    with pytest.raises(snapshot_pull.PullError):
        snapshot_pull.ssh_argv(pull)


def test_ssh_argv_is_locked_down():
    argv = snapshot_pull.ssh_argv({"host": "keeper.example.test", "user": "s", "identity": "/k"})
    assert argv[-2:] == ["--", "keeper.example.test"]      # no remote command, host after --
    for opt in ("BatchMode=yes", "StrictHostKeyChecking=yes", "ClearAllForwardings=yes",
                "RequestTTY=no", "IdentitiesOnly=yes", "PermitLocalCommand=no"):
        assert opt in argv
    assert {"-T", "-x", "-a"} <= set(argv) and argv[argv.index("-F") + 1] == "none"


def test_end_to_end_through_a_fake_ssh(keeper, reader, tmp_path, monkeypatch, capsys):
    """_fetch + CLI over a stand-in ssh that runs the forced command, as sshd would."""
    keeper.write(11)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "ssh"
    fake.write_text(f"#!/bin/sh\nSSH_ORIGINAL_COMMAND='rm -rf /' PYTHONPATH={SRC} "
                    f"exec {sys.executable} -m plangraph.keeper.snapshot_serve --config {keeper.cfg}\n")
    fake.chmod(0o700)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    assert cli.main(["--config", str(reader.cfg), "sync", "pull"]) == 0
    assert "head_seq 11" in capsys.readouterr().out
    fake.write_text("#!/bin/sh\necho 'ssh: connect refused' >&2\nexit 255\n")
    assert cli.main(["--config", str(reader.cfg), "sync", "pull"]) == 3
    assert snapshot_io.read_snapshot(reader.snap, str(keeper.pub))["head_seq"] == 11


def test_fetch_caps_the_answer_size(monkeypatch):
    monkeypatch.setattr(snapshot_serve, "MAX_BYTES", 10)
    with pytest.raises(snapshot_pull.PullError, match="larger"):
        snapshot_pull._fetch([sys.executable, "-c", "print('x' * 100000)"])


def test_fetch_times_out(monkeypatch):
    with pytest.raises(snapshot_pull.Unreachable):
        snapshot_pull._fetch([sys.executable, "-c", "import time; time.sleep(30)"], timeout=1)


def test_config_expands_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = config_mod.load_config(_json(tmp_path / "k.json", {
        "snapshot": "~/plan.json", "snapshot_pubkey": "~/k.pub"}))
    assert str(cfg.snapshot) == str(tmp_path / "plan.json")
    assert cfg.snapshot_pubkey == str(tmp_path / "k.pub")


def test_keeper_snapshot_carries_head_seq(tmp_path):
    from keeper_util import World, seed
    world = World(tmp_path)
    try:
        seed(world, [("A", [])])
        snap = world.store.snapshot(world.p("router"))
        last = world.store.conn.execute("SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        assert (snap["head_seq"], snap["head"]) == (last[0], last[1]) and last[0] > 0
    finally:
        world.close()
