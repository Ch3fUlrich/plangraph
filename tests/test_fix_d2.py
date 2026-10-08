"""FIX-D / FIX-D2: Waives:/Closes: keyword split and the no-redirect opener."""
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import apply_batch  # noqa: E402
from test_fix_d import _batch_path, _src_digest  # noqa: E402
from plangraph.keeper.evidence_decisions import DecisionsAdapter  # noqa: E402


# L4: Waives:/Closes: capture stops at next <Word>: keyword
def test_waives_closes_stops_at_next_keyword(tmp_path):
    text = ("| ID | Note | Scope |\n"
            "|---|---|---|\n"
            "| D-100 | test | Waives: demo:A Closes: demo:B |\n")
    adapter = DecisionsAdapter(tmp_path / "DECISIONS.md")
    adapter._path.write_text(text, encoding="utf-8")
    assert adapter.waives("D-100", "demo:A") is True
    assert adapter.waives("D-100", "demo:B") is False
    closes = adapter.closes("D-100")
    assert "demo:B" in closes and "demo:A" not in closes


def test_waives_multiple_keywords_in_cell(tmp_path):
    text = ("| ID | Note | Scope |\n"
            "|---|---|---|\n"
            "| D-101 | test | Waives: demo:A Closes: demo:B Waives: demo:C |\n")
    adapter = DecisionsAdapter(tmp_path / "DECISIONS.md")
    adapter._path.write_text(text, encoding="utf-8")
    assert adapter.waives("D-101", "demo:A") is True
    assert adapter.waives("D-101", "demo:C") is True
    assert adapter.waives("D-101", "demo:B") is False
    closes = adapter.closes("D-101")
    assert "demo:B" in closes and "demo:A" not in closes and "demo:C" not in closes


def test_closes_stops_at_next_keyword(tmp_path):
    text = ("| ID | Note | Scope |\n"
            "|---|---|---|\n"
            "| D-102 | test | Closes: demo:A Waives: demo:B |\n")
    adapter = DecisionsAdapter(tmp_path / "DECISIONS.md")
    adapter._path.write_text(text, encoding="utf-8")
    closes = adapter.closes("D-102")
    assert "demo:A" in closes and "demo:B" not in closes
    assert adapter.waives("D-102", "demo:B") is True
    assert adapter.waives("D-102", "demo:A") is False


def test_keyword_boundary_not_colon_in_url(tmp_path):
    text = ("| ID | Note | Scope |\n"
            "|---|---|---|\n"
            "| D-103 | test | Waives: demo:A See https://example.com for details |\n")
    adapter = DecisionsAdapter(tmp_path / "DECISIONS.md")
    adapter._path.write_text(text, encoding="utf-8")
    assert adapter.waives("D-103", "demo:A") is True


# F2: Waives/Closes capture stops at next <Word>: keyword
def test_waives_closes_mixed_keywords(tmp_path):
    # "Waives: demo:A Note: demo:B" -> waives("D-1", "demo:B") False, demo:A True
    text = ("| ID | Note | Scope |\n"
            "|---|---|---|\n"
            "| D-1 | test | Waives: demo:A Note: demo:B |\n")
    adapter = DecisionsAdapter(tmp_path / "DECISIONS.md")
    adapter._path.write_text(text, encoding="utf-8")
    assert adapter.waives("D-1", "demo:A") is True
    assert adapter.waives("D-1", "demo:B") is False
    
    # "Waives: demo:A demo:B" -> both True (project:ID tokens still capture)
    text = ("| ID | Note | Scope |\n"
            "|---|---|---|\n"
            "| D-2 | test | Waives: demo:A demo:B |\n")
    adapter = DecisionsAdapter(tmp_path / "DECISIONS.md")
    adapter._path.write_text(text, encoding="utf-8")
    assert adapter.waives("D-2", "demo:A") is True
    assert adapter.waives("D-2", "demo:B") is True


# F3: json.dumps(..., allow_nan=False) refuses NaN/Infinity
def test_prevalidate_refuses_nan_inf(tmp_path):
    src, dig = _src_digest(tmp_path, "naninf")
    token_file = tmp_path / "token"
    token_file.write_text("VALIDTOKEN123", encoding="utf-8")
    
    # float('nan') in fields
    ops = [{"op": "edit", "key": "demo:A", "fields": {"value": float("nan")}}]
    batch_path = _batch_path(tmp_path, src, dig, ops=ops)
    out, err = io.StringIO(), io.StringIO()
    rc = apply_batch.main([str(batch_path), "--url", "http://x", "--token-file", str(token_file)],
                          out=out, err=err)
    assert rc == 1
    out_err = out.getvalue() + err.getvalue()
    assert "op 1" in out_err
    assert "resume with --from 1" in out_err
    assert "cannot encode" in out_err.lower() or "nan" in out_err.lower()
    assert "Traceback" not in out_err
    
    # float('inf') in fields
    ops = [{"op": "edit", "key": "demo:A", "fields": {"value": float("inf")}}]
    batch_path = _batch_path(tmp_path, src, dig, ops=ops)
    out, err = io.StringIO(), io.StringIO()
    rc = apply_batch.main([str(batch_path), "--url", "http://x", "--token-file", str(token_file)],
                          out=out, err=err)
    assert rc == 1
    out_err = out.getvalue() + err.getvalue()
    assert "op 1" in out_err
    assert "resume with --from 1" in out_err
    assert "Traceback" not in out_err
    
    # float('-inf') in fields
    ops = [{"op": "edit", "key": "demo:A", "fields": {"value": float("-inf")}}]
    batch_path = _batch_path(tmp_path, src, dig, ops=ops)
    out, err = io.StringIO(), io.StringIO()
    rc = apply_batch.main([str(batch_path), "--url", "http://x", "--token-file", str(token_file)],
                          out=out, err=err)
    assert rc == 1
    out_err = out.getvalue() + err.getvalue()
    assert "op 1" in out_err
    assert "resume with --from 1" in out_err
    assert "Traceback" not in out_err


# F3: pre-validation loop json.dumps catches non-JSON-encodable values (date, set)
def test_prevalidate_json_encodable(tmp_path):
    src, dig = _src_digest(tmp_path, "jsonbad")
    token_file = tmp_path / "token"
    token_file.write_text("VALIDTOKEN123", encoding="utf-8")
    
    # YAML date in fields - not JSON encodable
    import datetime
    ops = [{"op": "edit", "key": "demo:A", "fields": {"date": datetime.date(2024, 1, 1)}}]
    batch_path = _batch_path(tmp_path, src, dig, ops=ops)
    out, err = io.StringIO(), io.StringIO()
    rc = apply_batch.main([str(batch_path), "--url", "http://x", "--token-file", str(token_file)],
                          out=out, err=err)
    assert rc == 1
    out_err = out.getvalue() + err.getvalue()
    assert "op 1" in out_err
    assert "resume with --from 1" in out_err
    assert "cannot encode" in out_err.lower() or "date" in out_err.lower()
    assert "Traceback" not in out_err
    
    # set in ref - not JSON encodable
    ops = [{"op": "add_evidence", "key": "demo:A", "kind": "decision", "ref": {"tags": {"a", "b"}}}]
    batch_path = _batch_path(tmp_path, src, dig, ops=ops)
    out, err = io.StringIO(), io.StringIO()
    rc = apply_batch.main([str(batch_path), "--url", "http://x", "--token-file", str(token_file)],
                          out=out, err=err)
    assert rc == 1
    out_err = out.getvalue() + err.getvalue()
    assert "op 1" in out_err
    assert "resume with --from 1" in out_err
    assert "Traceback" not in out_err


# F1: read_token refuses non-UTF-8
def test_read_token_refuses_non_utf8(tmp_path):
    src, dig = _src_digest(tmp_path, "tok")
    batch_path = _batch_path(tmp_path, src, dig)
    t = tmp_path / "t.token"
    t.write_bytes(b"abc\xffdef")  # Invalid UTF-8
    out, err = io.StringIO(), io.StringIO()
    rc = apply_batch.main([str(batch_path), "--url", "http://x", "--token-file", str(t)],
                          out=out, err=err)
    assert rc == 2
    # Only exception class name, no byte/position text
    out_err = out.getvalue() + err.getvalue()
    assert "UnicodeDecodeError" in out_err
    assert "position" not in out_err
    assert "byte" not in out_err


# F4c: redirect test - opener must not follow redirects
def test_redirect_not_followed(tmp_path):
    import http.server
    import threading
    import urllib.request
    
    # Track requests to second server
    second_server_requests = []
    ports = {}
    
    class FirstHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/v1/whoami":
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{ports['second']}/v1/whoami")
                self.end_headers()
                self.wfile.write(b"redirecting")
            else:
                self.send_response(404)
                self.end_headers()
        def log_message(self, format, *args):
            pass
    
    class SecondHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            second_server_requests.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"name": "router", "role": "router"}')
        def log_message(self, format, *args):
            pass
    
    # Start first server (ephemeral port)
    server1 = http.server.HTTPServer(("127.0.0.1", 0), FirstHandler)
    t1 = threading.Thread(target=server1.serve_forever, daemon=True)
    t1.start()
    
    # Start second server (ephemeral port)
    server2 = http.server.HTTPServer(("127.0.0.1", 0), SecondHandler)
    ports["second"] = server2.server_address[1]
    t2 = threading.Thread(target=server2.serve_forever, daemon=True)
    t2.start()
    
    try:
        # Create batch and token
        src, dig = _src_digest(tmp_path, "redir")
        batch_path = _batch_path(tmp_path, src, dig)
        token_file = tmp_path / "token"
        token_file.write_text("TESTTOKEN123", encoding="utf-8")
        
        # Run apply_batch with first server URL
        out, err = io.StringIO(), io.StringIO()
        rc = apply_batch.main([
            str(batch_path), 
            "--url", f"http://127.0.0.1:{server1.server_address[1]}",
            "--token-file", str(token_file)
        ], out=out, err=err)
        
        out_val = out.getvalue()
        err_val = err.getvalue()
        
        # Should fail (rc != 0) because redirect is not followed
        assert rc != 0, f"Expected failure, got rc={rc}: {out_val} {err_val}"
        
        # Second server should have received NO requests
        assert len(second_server_requests) == 0, f"Second server received requests: {second_server_requests}"
        
        # Token must not appear in output
        assert "TESTTOKEN123" not in out_val
        assert "TESTTOKEN123" not in err_val
    finally:
        server1.shutdown()
        server1.server_close()
        server2.shutdown()
        server2.server_close()
