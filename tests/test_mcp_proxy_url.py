"""PG-PROXY-ACCESS: the MCP client behind the reverse proxy (https base URL)."""
from __future__ import annotations

import io
import json

from plangraph.keeper.mcp import HttpKeeper, cleartext_warning


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Opener:
    def __init__(self):
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        return _Resp(b"router-1 router")


def test_https_base_url_with_prefix_builds_v1_urls(tmp_path):
    token = tmp_path / "t.token"
    token.write_text("secret-token\n", encoding="utf-8")
    opener = _Opener()
    client = HttpKeeper("https://keeper.example.test/", str(token), opener=opener)
    text, err = client.call("GET", "whoami", {})
    assert (text, err) == ("router-1 router", False)
    req = opener.requests[0]
    assert req.full_url.startswith("https://keeper.example.test/v1/whoami?")
    assert req.get_header("Authorization") == "Bearer secret-token"
    client.call("POST", "record_run", {"key": "p:A", "run": {}})
    post = opener.requests[1]
    assert post.full_url == "https://keeper.example.test/v1/record_run"
    assert json.loads(post.data) == {"key": "p:A", "run": {}}


def test_cleartext_warning_only_for_remote_http():
    assert cleartext_warning("https://keeper.example.test") is None
    assert cleartext_warning("http://127.0.0.1:8765") is None
    assert cleartext_warning("http://localhost:8765") is None
    assert cleartext_warning("") is None
    warn = cleartext_warning("http://plain.example.test:8080")
    assert warn and "clear text" in warn and "plain.example.test" in warn
    assert "secret" not in warn
