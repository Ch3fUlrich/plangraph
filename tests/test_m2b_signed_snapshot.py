"""M2B-SIGNED-SNAPSHOT: ed25519 detached signature of plan.snapshot.json.

Mutations killed: no domain-separation context (raw-signature test), signature written
for the wrong bytes (tamper test), a group-readable key accepted, read_snapshot skipping
the check when a pubkey is configured, keygen overwriting an existing key.
"""
from __future__ import annotations

import base64
import os
import stat
from argparse import Namespace
from types import SimpleNamespace

import pytest

pytest.importorskip("cryptography")

from plangraph.keeper import cli_admin, snapshot_io, snapshot_sign  # noqa: E402


@pytest.fixture
def keys(tmp_path):
    priv, pub = tmp_path / "snap.key", tmp_path / "snap.pub"
    snapshot_sign.keygen(priv, pub)
    return priv, pub


def test_keygen_is_0600_and_never_overwrites(keys):
    priv, pub = keys
    assert stat.S_IMODE(priv.stat().st_mode) == 0o600
    assert b"PRIVATE KEY" in priv.read_bytes() and b"PUBLIC KEY" in pub.read_bytes()
    with pytest.raises(snapshot_sign.SignError):
        snapshot_sign.keygen(priv, pub.with_suffix(".pub2"))


def test_sign_verify_and_tamper(keys, tmp_path):
    priv, pub = keys
    body = b'{"items": []}'
    sig = snapshot_sign.sign(body, priv)
    assert snapshot_sign.verify(body, sig, pub)
    assert not snapshot_sign.verify(body + b" ", sig, pub)          # one byte more
    assert not snapshot_sign.verify(body, sig[:-4] + "AAAA", pub)    # altered signature
    assert not snapshot_sign.verify(body, "not base64!", pub)
    assert not snapshot_sign.verify(body, base64.b64encode(b"x" * 10).decode(), pub)
    other_priv, other_pub = tmp_path / "o.key", tmp_path / "o.pub"
    snapshot_sign.keygen(other_priv, other_pub)
    assert not snapshot_sign.verify(body, sig, other_pub)            # another key


def test_signature_is_domain_separated(keys):
    priv, pub = keys
    from cryptography.hazmat.primitives import serialization
    key = serialization.load_pem_private_key(priv.read_bytes(), password=None)
    raw = base64.b64encode(key.sign(b"payload")).decode()            # no context prefix
    assert not snapshot_sign.verify(b"payload", raw, pub)


def test_group_readable_private_key_is_refused(keys):
    priv, _ = keys
    os.chmod(priv, 0o640)
    with pytest.raises(snapshot_sign.SignError, match="chmod 600"):
        snapshot_sign.sign(b"x", priv)


def _server(tmp_path, key=None):
    store = SimpleNamespace(snapshot=lambda principal: {"items": [{"key": "p:A"}]})
    config = SimpleNamespace(snapshot=str(tmp_path / "plan.snapshot.json"), signing_key=key)
    return SimpleNamespace(store=store, config=config)


def test_write_snapshot_signs_and_read_snapshot_verifies(keys, tmp_path):
    priv, pub = keys
    server = _server(tmp_path, str(priv))
    snapshot_io.write_snapshot(server, None)
    path = tmp_path / "plan.snapshot.json"
    assert snapshot_sign.sig_path(path).is_file()
    assert snapshot_io.read_snapshot(path, str(pub))["items"][0]["key"] == "p:A"
    path.write_bytes(path.read_bytes().replace(b"p:A", b"p:B"))      # tamper
    with pytest.raises(snapshot_sign.SignError):
        snapshot_io.read_snapshot(path, str(pub))
    assert snapshot_io.read_snapshot(path)["items"][0]["key"] == "p:B"  # no pubkey: unchecked


def test_unsigned_snapshot_refused_when_a_pubkey_is_configured(keys, tmp_path):
    _, pub = keys
    snapshot_io.write_snapshot(_server(tmp_path), None)               # no signing key
    path = tmp_path / "plan.snapshot.json"
    assert not snapshot_sign.sig_path(path).exists()
    with pytest.raises(snapshot_sign.SignError):
        snapshot_io.read_snapshot(path, str(pub))


def test_admin_verify_snapshot_exit_codes(keys, tmp_path, capsys):
    priv, pub = keys
    snapshot_io.write_snapshot(_server(tmp_path, str(priv)), None)
    path = str(tmp_path / "plan.snapshot.json")
    ok = cli_admin._cmd_verify_snapshot(Namespace(snapshot=path, pubkey=str(pub)), None)
    assert ok == 0 and "signature ok" in capsys.readouterr().out
    os.unlink(path + ".sig")
    assert cli_admin._cmd_verify_snapshot(Namespace(snapshot=path, pubkey=str(pub)), None) == 1
    assert cli_admin._cmd_verify_snapshot(Namespace(snapshot=None, pubkey=None), None) == 2
