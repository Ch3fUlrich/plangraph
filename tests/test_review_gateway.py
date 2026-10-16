"""Gateway review call: body, headers, masking, statuses, normalisation."""
import hashlib
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from plangraph.review import gateway as g

KEY = "k-%s-secret" % ("x" * 24)
KEY_ENV = "PLANGRAPH_REVIEW_KEY"


def make_server(capture, *, status=200, payload=None, headers=None, echo_key=False):
    """Serve one canned JSON reply on 127.0.0.1, recording the request."""
    headers = headers or {}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except ValueError:
                body = {}
            capture["path"] = self.path
            capture["body"] = body
            capture["headers"] = {k.lower(): v for k, v in self.headers.items()}
            if echo_key:
                token = self.headers.get("Authorization", "").replace("Bearer ", "")
                reply = dict(payload or {})
                reply["choices"] = [{
                    "message": {"content": "leak " + token + " done"},
                    "finish_reason": "stop",
                }]
            else:
                reply = dict(payload if payload is not None else {
                    "model": "served-m",
                    "choices": [{"message": {"content": "hello"},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1},
                })
            data = json.dumps(reply).encode("utf-8")
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), H)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def base_of(server):
    """Base URL (no path) for a running fake server."""
    return "http://127.0.0.1:%d" % server.server_port


def files_text(out):
    """Concatenated text of every file written into a seat dir."""
    return "".join(
        p.read_text(encoding="utf-8", errors="replace")
        for p in sorted(out.rglob("*")) if p.is_file()
    )


def test_body_has_no_tools_or_temperature(monkeypatch, tmp_path):
    """Body carries no tools key and no temperature by default."""
    cap = {}
    server = make_server(cap)
    monkeypatch.setenv(KEY_ENV, KEY)
    try:
        res = g.call("req-m", "prompt", url=base_of(server))
    finally:
        server.shutdown(); server.server_close()
    assert res.status == 200
    assert "tools" not in cap["body"]
    assert "temperature" not in cap["body"]
    assert cap["body"]["stream"] is False
    assert cap["body"]["max_tokens"] == g.DEFAULT_MAX_TOKENS
    body = g.request_body("m", "p")
    assert "tools" not in body and "temperature" not in body
    assert g.request_body("m", "p", temperature=0.2)["temperature"] == 0.2


def test_bearer_sent_but_files_mask_key(monkeypatch, tmp_path):
    """Bearer header is sent; echoed key never lands in written files."""
    cap = {}
    server = make_server(cap, headers={"x-correlation-id": "c1"}, echo_key=True)
    monkeypatch.setenv(KEY_ENV, KEY)
    try:
        res = g.call("req-m", "prompt", url=base_of(server))
        g.write_seat_dir(tmp_path, res)
    finally:
        server.shutdown(); server.server_close()
    assert cap["headers"]["authorization"] == "Bearer " + KEY
    assert SESSION_OK(cap)
    assert KEY not in files_text(tmp_path)
    assert "[redacted]" in (tmp_path / "review.txt").read_text()


def SESSION_OK(cap):
    """Review session header uses the gateway-neutral name."""
    return "x-review-session-id" in cap["headers"]


def test_happy_200_names_only(monkeypatch, tmp_path):
    """Served model differs; only header names are recorded."""
    cap = {}
    payload = {"model": "served-m", "choices": [
        {"message": {"content": "hi"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 2}}
    server = make_server(cap, payload=payload,
                         headers={"x-correlation-id": "corr-9",
                                  "X-Secret-Value": "shh"})
    monkeypatch.setenv(KEY_ENV, KEY)
    try:
        res = g.call("req-m", "prompt here", url=base_of(server))
        g.write_seat_dir(tmp_path, res)
    finally:
        server.shutdown(); server.server_close()
    assert res.status == 200
    assert res.served_model == "served-m" != res.requested_model
    assert res.correlation_id == "corr-9"
    assert "x-correlation-id" in res.response_header_names
    assert "corr-9" not in json.dumps(res.to_evidence()["response_header_names"])
    assert res.prompt_sha256 == hashlib.sha256(b"prompt here").hexdigest()
    assert KEY not in files_text(tmp_path)


def test_length_empty_no_raise(monkeypatch):
    """Empty content with finish length yields empty text, no raise."""
    cap = {}
    payload = {"model": "s", "choices": [
        {"message": {"content": ""}, "finish_reason": "length"}]}
    server = make_server(cap, payload=payload)
    monkeypatch.setenv(KEY_ENV, KEY)
    try:
        res = g.call("m", "p", url=base_of(server))
    finally:
        server.shutdown(); server.server_close()
    assert res.text == "" and res.finish_reason == "length"


def test_list_parts_content(monkeypatch):
    """List-of-parts content joins text parts only."""
    cap = {}
    payload = {"model": "s", "choices": [{"message": {"content": [
        {"type": "text", "text": "a"}, {"type": "x", "n": 1},
        {"text": "b"}]}, "finish_reason": "stop"}]}
    server = make_server(cap, payload=payload)
    monkeypatch.setenv(KEY_ENV, KEY)
    try:
        res = g.call("m", "p", url=base_of(server))
    finally:
        server.shutdown(); server.server_close()
    assert res.text == "ab"


def test_500_writes_error(monkeypatch, tmp_path):
    """Non-200 becomes a value with truncated error plus error.json."""
    cap = {}
    server = make_server(cap, status=500,
                         payload={"oops": "x" * 5000})
    monkeypatch.setenv(KEY_ENV, KEY)
    try:
        res = g.call("m", "p", url=base_of(server))
        g.write_seat_dir(tmp_path, res)
    finally:
        server.shutdown(); server.server_close()
    assert res.status == 500 and res.text == ""
    assert res.error.startswith("HTTP 500")
    assert len(res.error) <= len("HTTP 500: ") + 200
    err = json.loads((tmp_path / "error.json").read_text())
    assert err["status"] == 500
    assert KEY not in files_text(tmp_path)


def test_connection_refused_status_0(monkeypatch):
    """Unreachable gateway yields status 0 with an exception type name."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    monkeypatch.setenv(KEY_ENV, KEY)
    res = g.call("m", "p", url="http://127.0.0.1:%d" % port, timeout=5)
    assert res.status == 0 and res.error


def test_missing_key_names_var_not_value(monkeypatch):
    """Missing key raises naming the var, never the value."""
    monkeypatch.delenv(KEY_ENV, raising=False)
    with pytest.raises(g.MissingKey) as exc:
        g.call("m", "p", url="http://127.0.0.1:1")
    assert KEY_ENV in str(exc.value)
    assert KEY not in str(exc.value)
    assert issubclass(g.MissingKey, RuntimeError)


def test_url_normalisation(monkeypatch):
    """Path-less URLs gain /v1; existing paths are kept."""
    assert g.gateway_base_url("http://127.0.0.1:1").endswith("/v1")
    assert g.gateway_base_url("http://127.0.0.1:1/").endswith("/v1")
    assert g.gateway_base_url("http://127.0.0.1:1/v1").endswith("/v1")
    assert g.gateway_base_url("http://127.0.0.1:1/custom").endswith("/custom")
    cap = {}
    server = make_server(cap)
    monkeypatch.setenv(KEY_ENV, KEY)
    try:
        g.call("m", "p", url=base_of(server))
        assert cap["path"] == "/v1/chat/completions"
        g.call("m", "p", url=base_of(server) + "/custom")
        assert cap["path"] == "/custom/chat/completions"
    finally:
        server.shutdown(); server.server_close()


def test_evidence_keys_exact_and_extra(monkeypatch, tmp_path):
    """Evidence keys match the example minus verdict/test-gaming."""
    cap = {}
    server = make_server(cap, headers={"x-correlation-id": "c"})
    monkeypatch.setenv(KEY_ENV, KEY)
    try:
        res = g.call("req", "p", url=base_of(server))
    finally:
        server.shutdown(); server.server_close()
    assert set(res.to_evidence()) == {
        "requested_model", "served_model", "status", "finish_reason",
        "correlation_id", "response_header_names", "session_tag",
        "run_id", "prompt_sha256", "tokens", "started", "finished",
    }
    g.write_seat_dir(tmp_path, res, extra={"k": "v"})
    doc = json.loads((tmp_path / "evidence.json").read_text())
    assert doc["k"] == "v" and doc["status"] == 200
    assert not (tmp_path / "error.json").exists()
