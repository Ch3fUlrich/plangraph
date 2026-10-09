"""M2B-SIGNED-SNAPSHOT: ed25519 detached signatures for ``plan.snapshot.json``.

The keeper signs the exact snapshot bytes it writes (``<snapshot>.sig``, base64 of a
64-byte ed25519 signature over ``b"plangraph-snapshot-v1\\n" + bytes``); readers that
hold the public key refuse an unsigned, tampered or wrongly-signed snapshot.

Dependency: ``cryptography`` (>= 41.0.7, Apache-2.0 OR BSD-3-Clause), imported lazily
so a keeper without a configured key runs without it.  The private key file must not
be readable by group/other; key values are never printed or logged.
"""
from __future__ import annotations

import base64
import os
import stat
from pathlib import Path

__all__ = ["SignError", "keygen", "sign", "verify", "sig_path", "CONTEXT"]

#: Domain separation: a signature over a snapshot cannot be replayed as another artefact.
CONTEXT = b"plangraph-snapshot-v1\n"


class SignError(Exception):
    """A key or signature problem (message never carries key material)."""


def _ed25519():
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ed25519
    except ImportError as exc:  # pragma: no cover - depends on the host
        raise SignError("the 'cryptography' package is required for snapshot signing") from exc
    return serialization, ed25519


def sig_path(snapshot_path) -> Path:
    return Path(str(snapshot_path) + ".sig")


def _private_key(path):
    serialization, ed25519 = _ed25519()
    path = Path(path)
    # One open, then fstat + read on that same fd: no stat-then-read window, no symlink.
    try:
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise SignError(f"signing key not readable: {path}") from exc
    with os.fdopen(fd, "rb") as handle:
        if os.fstat(handle.fileno()).st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise SignError(f"signing key {path} must not be accessible by group/other (chmod 600)")
        pem = handle.read()
    try:
        key = serialization.load_pem_private_key(pem, password=None)
    except (ValueError, TypeError) as exc:
        raise SignError("signing key is not a readable PEM private key") from exc
    if not isinstance(key, ed25519.Ed25519PrivateKey):
        raise SignError("signing key is not an ed25519 private key")
    return key


def _public_key(path):
    serialization, ed25519 = _ed25519()
    try:
        key = serialization.load_pem_public_key(Path(path).read_bytes())
    except (OSError, ValueError) as exc:
        raise SignError(f"public key not readable: {path}") from exc
    if not isinstance(key, ed25519.Ed25519PublicKey):
        raise SignError("public key is not an ed25519 key")
    return key


def keygen(private_path, public_path) -> None:
    """Create a new key pair; refuses to overwrite either file."""
    serialization, ed25519 = _ed25519()
    private_path, public_path = Path(private_path), Path(public_path)
    if os.path.abspath(private_path) == os.path.abspath(public_path):
        raise SignError("private and public key paths must differ")
    for p in (private_path, public_path):
        if p.exists() or p.is_symlink():
            raise SignError(f"refusing to overwrite {p}")
    key = ed25519.Ed25519PrivateKey.generate()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    fd = os.open(str(private_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(pem)
    fd = os.open(str(public_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(fd, "wb") as handle:   # O_EXCL + explicit mode: no overwrite, umask-proof
        handle.write(key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))


def sign(data: bytes, private_path) -> str:
    """Base64 signature of *data* (the exact snapshot bytes)."""
    return base64.b64encode(_private_key(private_path).sign(CONTEXT + data)).decode("ascii")


def verify(data: bytes, signature: str, public_path) -> bool:
    """True iff *signature* (base64) is a valid signature of *data* under the public key."""
    key = _public_key(public_path)
    try:
        raw = base64.b64decode((signature or "").strip(), validate=True)
    except (ValueError, TypeError):
        return False
    if len(raw) != 64:
        return False
    from cryptography.exceptions import InvalidSignature
    try:
        key.verify(raw, CONTEXT + data)
    except InvalidSignature:
        return False
    return True
