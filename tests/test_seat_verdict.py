"""tools/seat_verdict.py: strict verdict parse, family gate, JSON-boolean send."""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import seat_verdict as sv  # noqa: E402

TOKEN = "dummy"


@pytest.mark.parametrize("text,want", [
    ("blah\nVERDICT: PASS\n", True),
    ("**VERDICT: PASS**", True),
    ("**VERDICT:** approve", True),
    ("verdict: Pass - looks good, thanks", True),
    ("## Verdict: FIX\nthe bug", False),
    ("VERDICT: halt", False),
    ("VERDICT: Changes requested", False),
    ("VERDICT: reject.", False),
    ("VERDICT: PASS\nmore\nVERDICT: FIX\nwhy", False),
    ("VERDICT: FIX\nwhy\nVERDICT: PASS", True),
    ("VERDICT: pass | fix | halt", None),
    ("VERDICT: pass/fix", None),
    ("VERDICT: maybe", None),
    ("VERDICT: PASS\nVERDICT: <pass | fix | halt>", None),
    ("I think this should pass", None),
    ("My verdict: pass", None),
    ("VERDICT:", None),
    ("", None),
])
def test_parse(text, want):
    assert sv.parse_verdict(text)[0] is want


def test_findings_after_else_before():
    text = "early notes\nVERDICT: FIX\n" + "x" * 700
    assert sv.findings(text, sv.parse_verdict(text)[1]) == "x" * 600
    text = "only before\nVERDICT: FIX"
    assert sv.findings(text, sv.parse_verdict(text)[1]) == "only before"


@pytest.mark.parametrize("model,fam", [
    ("gpt-oss-120b", "openai"), ("openai/gpt-5", "openai"), ("o3-mini", "openai"),
    ("mimo-v2", "xiaomi"), ("llama-4", "meta"), ("muse-spark", "meta"),
    ("deepseek-r1", "deepseek"), ("qwen3-max", "alibaba"),
    ("gemini-3-pro", "google"), ("claude-sonnet-5", "anthropic"),
    ("claude-3", "anthropic"),
    ("glm-5.3-flash-free", "zhipu"), ("zhipu/glm-5", "zhipu"),
    ("nvidia/nemotron-3-ultra-550b-a55b:free", "nvidia"),
    ("glm-qwen-merge", None),
    ("opus", "anthropic"), ("haiku-4", "anthropic"),
    ("mystery-1", None), (None, None),
    ("gpt-claude-3", None), ("claude-via-gpt-router", None),
    ("deepseek-llama-distill", None), ("deepseek-r1-distill-llama", None)])
def test_family(model, fam):
    assert sv.model_family(model) == fam


def seat(tmp_path, review, **ev):
    """Write a seat dir and token dir under tmp_path; return the CLI args."""
    d = tmp_path / "seat"
    d.mkdir()
    evidence = {"requested_model": "gpt-oss-120b", "served_model": "gpt-oss-120b",
                "finish_reason": "stop", "verdict": "pass", "test_gaming": "no",
                "correlation_id": "corr-1", "prompt_sha256": "ab" * 32}
    evidence.update(ev)
    (d / "evidence.json").write_text(json.dumps(evidence))
    (d / "review.txt").write_text(review)
    tokens = tmp_path / "tok"
    tokens.mkdir()
    (tokens / "reviewer-openai.token").write_text(TOKEN + "\n")
    return ["--key", "demo:A", "--seat-dir", str(d), "--writer-family", "meta",
            "--token-dir", str(tokens)]


def run(tmp_path, name, review, writer=None, extra=(), **ev):
    sub = tmp_path / name
    sub.mkdir()
    argv = seat(sub, review, **ev)
    if writer is not None:
        argv[argv.index("--writer-family") + 1] = writer
    return sv.main(argv + list(extra))


def test_refusals(tmp_path, capsys):
    assert run(tmp_path, "a", "VERDICT: PASS", served_model="claude-opus",
               extra=("--dry-run",)) == 0
    capsys.readouterr()
    assert run(tmp_path, "b", "VERDICT: PASS", served_model="mystery") == 2
    assert run(tmp_path, "c", "VERDICT: PASS", writer="openai") == 2
    assert run(tmp_path, "d", "VERDICT: PASS", served_model="claude-opus",
               writer="anthropic") == 2
    assert run(tmp_path, "e", "VERDICT: PASS", served_model="claude-opus",
               writer="mystery-x") == 2
    assert capsys.readouterr().out == ""


def test_claude_seat_writer_gate(tmp_path):
    assert run(tmp_path, "a", "VERDICT: PASS", served_model="claude-opus",
               writer="xiaomi", extra=("--dry-run",)) == 0
    assert run(tmp_path, "b", "VERDICT: PASS", served_model="claude-opus",
               writer="anthropic") == 2
    assert run(tmp_path, "c", "VERDICT: PASS", served_model="claude-opus",
               writer="xiaomi,anthropic") == 2
    assert run(tmp_path, "d", "VERDICT: PASS", served_model="claude-opus",
               writer="unknownfam") == 2
    assert run(tmp_path, "e", "VERDICT: PASS", served_model="claude-opus",
               writer="") == 2


def test_writer_coverage_gate(tmp_path, capsys):
    assert run(tmp_path, "a", "VERDICT: PASS", writer="openai") == 2
    assert run(tmp_path, "b", "VERDICT: PASS", writer="openai,xiaomi",
               extra=("--dry-run",)) == 0
    assert "covers: xiaomi" in capsys.readouterr().out


def test_coverage():
    assert sv.coverage(["xiaomi"], ["anthropic", "anthropic"], need=2) == ["xiaomi"]
    assert sv.coverage(["xiaomi"], ["anthropic", "alibaba"], need=2) == []
    assert sv.coverage(["xiaomi"], ["anthropic", "anthropic", "alibaba"],
                       need=2) == []
    assert sv.coverage(["xiaomi"], ["anthropic", "anthropic"], need=1,
                       sensitive=True) == ["xiaomi"]
    assert sv.coverage(["xiaomi", "alibaba"], ["alibaba", "google"],
                       need=2) == ["alibaba"]
    assert sv.coverage(["xiaomi"], ["anthropic"], need=1, sensitive=False) == []


def test_new_families_not_refused(tmp_path):
    assert run(tmp_path, "z", "VERDICT: PASS",
               served_model="glm-5.3-flash-free",
               extra=("--dry-run",)) == 0
    assert run(tmp_path, "n", "VERDICT: PASS",
               served_model="nvidia/nemotron-3-ultra-550b-a55b:free",
               extra=("--dry-run",)) == 0
    assert run(tmp_path, "o", "VERDICT: PASS",
               served_model="claude-opus", writer="anthropic",
               extra=("--dry-run",)) == 2


def test_multi_family_refused_before_send(tmp_path, capsys):
    for i, m in enumerate(("gpt-claude-3", "deepseek-llama-distill")):
        assert run(tmp_path, f"m{i}", "VERDICT: PASS", served_model=m) == 2
        assert run(tmp_path, f"f{i}", "VERDICT: FIX\nx", served_model=m) == 2
    assert capsys.readouterr().out == ""


def test_served_model_falls_back_to_requested(tmp_path):
    assert run(tmp_path, "a", "VERDICT: PASS", extra=["--dry-run"], served_model=None) == 0
    assert run(tmp_path, "b", "VERDICT: PASS", writer="openai", served_model=None) == 2


def test_no_verdict_and_pass_gates(tmp_path):
    assert run(tmp_path, "a", "VERDICT: pass | fix") == 3
    dry = ["--dry-run"]
    assert run(tmp_path, "b", "VERDICT: PASS", extra=dry, finish_reason="length") == 4
    assert run(tmp_path, "c", "VERDICT: PASS", extra=dry, test_gaming="yes") == 4
    # a fix verdict is still recorded when the call was truncated
    assert run(tmp_path, "d", "VERDICT: FIX\nbad", extra=dry, finish_reason="length") == 0


@pytest.mark.parametrize("gaming", [None, "missing", "unsure", "yes", "", 0, False])
def test_pass_needs_test_gaming_no(tmp_path, gaming):
    dry = ["--dry-run"]
    assert run(tmp_path, "p", "VERDICT: PASS", extra=dry, test_gaming=gaming) == 4
    assert run(tmp_path, "f", "VERDICT: FIX\nbad", extra=dry, test_gaming=gaming) == 0


def test_pass_test_gaming_no_normalised(tmp_path):
    assert run(tmp_path, "a", "VERDICT: PASS", extra=["--dry-run"], test_gaming=" No ") == 0


def test_fix_sent_with_test_gaming_missing(tmp_path, monkeypatch):
    Stub.seen.clear()
    srv = HTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.delenv("PG_URL", raising=False)
        sub = tmp_path / "x"
        sub.mkdir()
        argv = seat(sub, "VERDICT: FIX\nbad", test_gaming=None)
        assert sv.main(argv + ["--url", f"http://127.0.0.1:{srv.server_port}"]) == 0
    finally:
        srv.shutdown()
        srv.server_close()
    (_, _, raw), = Stub.seen
    assert json.loads(raw)["ok"] is False


def test_payload_is_bool():
    assert type(sv.verdict_payload("k", True, "r")["ok"]) is bool
    assert type(sv.verdict_payload("k", False, "r")["ok"]) is bool
    for bad in ("false", 1, None):
        with pytest.raises(AssertionError):
            sv.verdict_payload("k", bad, "r")


def test_reason_format():
    ev = {"served_model": "m", "correlation_id": "c", "prompt_sha256": "0123456789abcdef"}
    assert sv.build_reason(ev, True, "VERDICT: PASS", 0) == "seat m PASS corr c prompt 0123456789ab"
    assert sv.build_reason(ev, False, "VERDICT: FIX\nwhy", 0).endswith("\nwhy")


def test_dry_run_redacts(tmp_path, capsys):
    assert sv.main(seat(tmp_path, "VERDICT: PASS") + ["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert TOKEN not in out and "<redacted>" in out
    assert json.loads(out)["body"]["ok"] is True


class Stub(BaseHTTPRequestHandler):
    seen = []

    def do_POST(self):  # noqa: N802
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        Stub.seen.append((self.path, self.headers.get("Authorization"), raw))
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.mark.parametrize("review,literal", [("VERDICT: PASS", b'"ok": true'),
                                            ("VERDICT: FIX\nwhy", b'"ok": false')])
def test_send(tmp_path, capsys, monkeypatch, review, literal):
    Stub.seen.clear()
    srv = HTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.delenv("PG_URL", raising=False)
        argv = seat(tmp_path, review) + ["--url", f"http://127.0.0.1:{srv.server_port}"]
        assert sv.main(argv) == 0
    finally:
        srv.shutdown()
        srv.server_close()
    (path, auth, raw), = Stub.seen
    assert path == "/v1/verdict" and auth == f"Bearer {TOKEN}"
    assert literal in raw and json.loads(raw)["key"] == "demo:A"
    cap = capsys.readouterr()
    assert TOKEN not in cap.out + cap.err and "http=200" in cap.out
