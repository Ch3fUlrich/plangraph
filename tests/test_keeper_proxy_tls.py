"""PG-PROXY-ACCESS D-759 A: mint-certs.sh output and the mutual-TLS sidecar config."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROXY = ROOT / "deploy" / "keeper-proxy"

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl missing")


def _text(cert):
    return subprocess.run(["openssl", "x509", "-in", str(cert), "-noout", "-text"],
                          capture_output=True, text=True, check=True).stdout


def _verify(out, cert, purpose):
    return subprocess.run(["openssl", "verify", "-purpose", purpose, "-CAfile",
                           str(out / "ca.crt"), str(out / cert)],
                          capture_output=True, text=True)


def _mint(*args):
    subprocess.run(["sh", str(PROXY / "mint-certs.sh"), *map(str, args)], check=True,
                   capture_output=True)


@pytest.fixture(scope="module")
def minted(tmp_path_factory):
    ca = tmp_path_factory.mktemp("ca")
    out = tmp_path_factory.mktemp("tls")
    _mint("ca", ca)
    _mint("server", ca, out, "keeper.example.test", "203.0.113.7")
    _mint("client", ca, out)
    return out


def test_leaf_dir_holds_no_ca_key_and_private_keys_are_0600(minted):
    assert sorted(p.name for p in minted.iterdir()) == [
        "ca.crt", "client.crt", "client.key", "server.crt", "server.key"]
    assert (minted / "server.key").stat().st_mode & 0o077 == 0
    assert (minted / "client.key").stat().st_mode & 0o077 == 0


def test_ca_is_never_overwritten(tmp_path):
    _mint("ca", tmp_path)
    again = subprocess.run(["sh", str(PROXY / "mint-certs.sh"), "ca", str(tmp_path)],
                           capture_output=True, text=True)
    assert again.returncode != 0 and "refusing" in again.stderr


def test_server_cert_names_the_host_and_is_server_only(minted):
    text = _text(minted / "server.crt")
    assert "DNS:keeper.example.test" in text and "IP Address:203.0.113.7" in text
    assert "TLS Web Server Authentication" in text
    assert "TLS Web Client Authentication" not in text
    assert _verify(minted, "server.crt", "sslserver").returncode == 0
    assert _verify(minted, "server.crt", "sslclient").returncode != 0


def test_client_cert_is_client_only(minted):
    text = _text(minted / "client.crt")
    assert "TLS Web Client Authentication" in text
    assert "TLS Web Server Authentication" not in text
    assert _verify(minted, "client.crt", "sslclient").returncode == 0
    assert _verify(minted, "client.crt", "sslserver").returncode != 0


def test_a_foreign_ca_does_not_verify(minted, tmp_path):
    other = tmp_path / "other"
    _mint("ca", other)
    bad = subprocess.run(["openssl", "verify", "-CAfile", str(other / "ca.crt"),
                          str(minted / "server.crt")], capture_output=True, text=True)
    assert bad.returncode != 0


def test_nginx_requires_client_certs_and_modern_tls():
    conf = (PROXY / "nginx.conf").read_text(encoding="utf-8")
    assert "listen 192.168.178.73:8788 ssl;" in conf
    assert "ssl_verify_client on;" in conf
    assert "ssl_client_certificate /etc/nginx/tls/ca.crt;" in conf
    assert "ssl_protocols TLSv1.2 TLSv1.3;" in conf
    assert "proxy_pass http://127.0.0.1:8787/v1/;" in conf
    assert "listen 127.0.0.1:8789;" in conf  # healthcheck stays local and plain


def test_caddy_snippet_pins_the_ca_and_name_and_never_skips_verify():
    readme = (PROXY / "README.md").read_text(encoding="utf-8")
    assert "reverse_proxy https://coding.vm:8788" in readme
    assert "tls_trust_pool file /usr/local/etc/caddy/internal-ca/ca.crt" in readme
    assert "tls_server_name coding.vm" in readme
    assert "tls_client_auth" in readme
    snippet = readme.split("```", 2)[1]
    assert "insecure_skip_verify" not in snippet


def test_tls_material_is_git_ignored():
    assert (PROXY / ".gitignore").read_text(encoding="utf-8").split() == ["tls/"]
