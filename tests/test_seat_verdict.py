"""tools/seat_verdict.py: strict verdict parse, family gate, JSON-boolean send."""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import seat_verdict as sv  # noqa: E402
from test_review_verdict import LOW_SEVERITY_TABLE  # noqa: E402

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


@pytest.mark.parametrize("text,want_verdict,_", LOW_SEVERITY_TABLE)
def test_low_severity_verdict_table(text, want_verdict, _):
    want = True if want_verdict == "pass" else (False if want_verdict in ("fix", "halt") else None)
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
                "correlation_id": "corr-1", "prompt_sha256": "ab" * 32,
                "head_sha": "a" * 40, "run_id": "run-example"}
    evidence.update(ev)
    (d / "evidence.json").write_text(json.dumps(evidence))
    binding = {"key": "demo:A", "epoch": 1, "round": 1, "head": evidence["head_sha"],
               "seat_run": evidence["run_id"]}
    (d / "record.json").write_text(json.dumps({"request": {"verdict_binding": binding}}))
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


@pytest.mark.parametrize("model", ["qoder/Efficient", "efficient", "EFFICIENT"])
def test_qoder_efficient_end_turn_pass(tmp_path, capsys, model):
    assert run(tmp_path, "q", "VERDICT: pass", served_model=model,
               finish_reason="end_turn", test_gaming="no", extra=("--dry-run",)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["family"] == "alibaba"
    assert payload["body"]["ok"] is True
    assert payload["body"]["key"] == "demo:A"


@pytest.mark.parametrize("finish", ["length", "max_tokens", "content_filter",
                                   "tool_use", None, "", "END_TURN", "end_turn ", "missing"])
def test_pass_finish_reason_gate(tmp_path, capsys, finish):
    argv = seat(tmp_path, "VERDICT: pass", finish_reason=finish)
    if finish == "missing":
        path = Path(argv[argv.index("--seat-dir") + 1]) / "evidence.json"
        evidence = json.loads(path.read_text())
        del evidence["finish_reason"]
        path.write_text(json.dumps(evidence))
    assert sv.main(argv + ["--dry-run"]) == 4
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "stop" in captured.err and "end_turn" in captured.err


@pytest.mark.parametrize("model,family", [("inefficient", None),
    ("inefficient-x", None), ("efficient-llama", "meta"), ("qoder/unknownmodel", None)])
def test_efficient_alias_is_exact(tmp_path, capsys, model, family):
    assert sv.model_family(model) == family
    assert run(tmp_path, "u", "VERDICT: pass", served_model=model,
               finish_reason="end_turn", extra=("--dry-run",)) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert ("is unknown" if family is None else "covers no listed writer") in captured.err


@pytest.mark.parametrize("model", ["claude-3", "sonnet-5", "haiku-4"])
@pytest.mark.parametrize("finish", ["stop", "end_turn"])
def test_claude_alias_writer_rule(tmp_path, model, finish):
    for i, (writer, expected) in enumerate([("meta", 0), ("anthropic", 2),
            ("meta,anthropic", 2), ("unknownfam", 2), ("", 2)]):
        assert run(tmp_path, str(i), "VERDICT: pass", served_model=model,
                   finish_reason=finish, writer=writer, extra=("--dry-run",)) == expected


@pytest.mark.parametrize("gaming", ["yes", "unsure"])
def test_end_turn_still_refuses_test_gaming(tmp_path, capsys, gaming):
    assert run(tmp_path, "g", "VERDICT: pass", finish_reason="end_turn",
               test_gaming=gaming, extra=("--dry-run",)) == 4
    assert capsys.readouterr().out == ""


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


def test_unknown_writer_family_refused_for_any_seat(tmp_path):
    assert run(tmp_path, "u1", "VERDICT: PASS", writer="qwenn",
               extra=("--dry-run",)) == 2
    assert run(tmp_path, "u2", "VERDICT: PASS", writer="alibaba,qwenn",
               extra=("--dry-run",)) == 2
    assert run(tmp_path, "u3", "VERDICT: PASS", served_model="claude-opus",
               writer="qwenn", extra=("--dry-run",)) == 2
    assert run(tmp_path, "u4", "VERDICT: PASS", served_model="claude-opus",
               writer="alibaba", extra=("--dry-run",)) == 0


def test_coverage_sensitive_high_claude_plus_foreign_passes(tmp_path):
    assert sv.coverage(["xiaomi"], ["anthropic", "alibaba"], need=1,
                       sensitive=True) == []
    assert sv.coverage(["xiaomi"], ["anthropic", "alibaba"], need=2,
                       sensitive=True) == []
    assert sv.coverage(["xiaomi"], ["anthropic", "anthropic"], need=2,
                       sensitive=True) == ["xiaomi"]
    assert sv.coverage(["xiaomi"], ["anthropic", "alibaba", "google"],
                       need=2, sensitive=True) == []
    assert run(tmp_path, "s1", "VERDICT: PASS", served_model="claude-opus",
               writer="alibaba", extra=("--dry-run",)) == 0
    assert run(tmp_path, "s2", "VERDICT: PASS", writer="xiaomi",
               extra=("--dry-run",)) == 0


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
    assert [path for path, _, _ in Stub.seen] == ["/v1/bind_seat_verdict", "/v1/verdict"]
    assert all(json.loads(raw)["ok"] is False for _, _, raw in Stub.seen)


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
    (bind, binding_auth, bound), (path, auth, raw) = Stub.seen
    assert (bind, path) == ("/v1/bind_seat_verdict", "/v1/verdict")
    assert auth == binding_auth == f"Bearer {TOKEN}" and bound == raw
    assert literal in raw and json.loads(raw)["key"] == "demo:A"
    cap = capsys.readouterr()
    assert TOKEN not in cap.out + cap.err and "http=200" in cap.out


# D-2905 (2): request-side opencode store witness, interim until a
# response-side source (gateway logs) exists.
def test_request_side_opencode_store_witness_family_counts(tmp_path, capsys):
    assert run(tmp_path, "w1", "VERDICT: pass", served_model="unwitnessed",
               witness_kind="request-side-opencode-store",
               request_side_model="opencode/glm-5.3-flash-free",
               extra=("--dry-run",)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["family"] == "zhipu"
    reason = payload["body"]["reason"]
    assert "witness=request-side-opencode-store" in reason
    assert "opencode/glm-5.3-flash-free" in reason


def test_request_side_witness_refused_without_kind(tmp_path, capsys):
    assert run(tmp_path, "w2", "VERDICT: pass", served_model="unwitnessed",
               request_side_model="opencode/glm-5.3-flash-free") == 2
    err = capsys.readouterr().err
    assert "family None of model 'unwitnessed' is unknown" in err


@pytest.mark.parametrize("model", ["mimo/mimo-v2.6-pro", "opencode/glm-5.3"])
def test_request_side_witness_refused_non_opencode_free_model(tmp_path, capsys, model):
    assert run(tmp_path, "w3", "VERDICT: pass", served_model="unwitnessed",
               witness_kind="request-side-opencode-store",
               request_side_model=model) == 2
    err = capsys.readouterr().err
    assert "family None of model 'unwitnessed' is unknown" in err


@pytest.mark.parametrize("kind", ["served", "response-side-gateway", "", None, "Request-Side-Opencode-Store"])
def test_request_side_witness_refused_other_witness_kind(tmp_path, capsys, kind):
    assert run(tmp_path, "w4", "VERDICT: pass", served_model="unwitnessed",
               witness_kind=kind,
               request_side_model="opencode/glm-5.3-flash-free") == 2
    err = capsys.readouterr().err
    assert "family None of model 'unwitnessed' is unknown" in err


def test_served_model_witnessed_ignores_request_side(tmp_path, capsys):
    assert run(tmp_path, "w5", "VERDICT: pass", served_model="glm-5.3-flash-free",
               witness_kind="request-side-opencode-store",
               request_side_model="mimo/mimo-v2.6-pro",
               extra=("--dry-run",)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["family"] == "zhipu"
    assert "witness=" not in payload["body"]["reason"]
    assert "mimo/mimo-v2.6-pro" not in payload["body"]["reason"]


def test_build_reason_marks_request_side_witness():
    ev = {"served_model": "unwitnessed", "correlation_id": "c",
          "prompt_sha256": "0123456789abcdef"}
    marked = sv.build_reason(ev, True, "VERDICT: PASS", 0,
                             witness="opencode/glm-5.3-flash-free")
    assert "witness=request-side-opencode-store" in marked
    assert "opencode/glm-5.3-flash-free" in marked
    assert sv.build_reason(ev, True, "VERDICT: PASS", 0) == \
        "seat unwitnessed PASS corr c prompt 0123456789ab"


# D-2921 F4: the served check must take precedence even over an ADMISSIBLE
# request-side model (w5 cannot discriminate: its model is not admissible at
# all, so the outcome is identical without the served check).
def test_served_model_witness_precedes_admissible_request_side(tmp_path, capsys):
    assert run(tmp_path, "w6", "VERDICT: pass", served_model="gpt-oss-120b",
               witness_kind="request-side-opencode-store",
               request_side_model="opencode/glm-5.3-flash-free",
               extra=("--dry-run",)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["family"] == "openai"
    reason = payload["body"]["reason"]
    assert "witness=" not in reason
    assert "glm-5.3-flash-free" not in reason


def _process_seat(tmp_path, review, prompt, digest=None):
    import hashlib

    argv = seat(tmp_path, review, prompt_sha256=digest or hashlib.sha256(prompt.encode()).hexdigest())
    seat_dir = Path(argv[argv.index("--seat-dir") + 1])
    (seat_dir / "prompt.txt").write_text(prompt)
    return argv, seat_dir


PROCESS_PROMPT = "=== ITEM CONTEXT\nItem key: demo:A\nChanged public symbols: unavailable (request before acceptance)\n"
PROCESS_DIFF = "diff --git a/src/a.py b/src/a.py\n+x\n"
BRIEF_MED = "PROCESS src/plangraph/review/review_brief.md:26 field=public_symbols - brief omits public symbols"


def test_a_process_only_reply_of_a_seat_with_no_recorded_binding_is_a_counted_fix(tmp_path, capsys):
    # The dispatched diff scope is unknown without a verified review_binding, so the prompt's own
    # example headers or commands never turn a finding into a process hold (bound seats: record tests).
    argv, seat_dir = _process_seat(tmp_path, f"{BRIEF_MED}\nVERDICT: REQUEST_CHANGES\n",
                                   PROCESS_PROMPT + PROCESS_DIFF)
    assert sv.main(argv + ["--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["body"]["ok"] is False
    assert not (seat_dir / "process.json").exists()


def test_mixed_response_keeps_the_real_blocker_as_fix(tmp_path, capsys):
    argv, seat_dir = _process_seat(
        tmp_path, f"{BRIEF_MED}\nHIGH src/a.py:3 - real bug\nVERDICT: REQUEST_CHANGES\n",
        PROCESS_PROMPT + PROCESS_DIFF)
    assert sv.main(argv + ["--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["body"]["ok"] is False


def test_brief_finding_stays_a_fix_when_the_head_changes_the_brief(tmp_path, capsys):
    changed = "diff --git a/src/plangraph/review/review_brief.md b/src/plangraph/review/review_brief.md\n+x\n"
    argv, seat_dir = _process_seat(tmp_path, f"{BRIEF_MED}\nVERDICT: REQUEST_CHANGES\n",
                                   PROCESS_PROMPT + changed)
    assert sv.main(argv + ["--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["body"]["ok"] is False
    assert not (seat_dir / "process.json").exists()


def test_saved_prompt_with_another_digest_suppresses_nothing(tmp_path, capsys):
    argv, seat_dir = _process_seat(tmp_path, f"{BRIEF_MED}\nVERDICT: REQUEST_CHANGES\n",
                                   PROCESS_PROMPT + PROCESS_DIFF, digest="cd" * 32)
    assert sv.main(argv + ["--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["body"]["ok"] is False
    assert not (seat_dir / "process.json").exists()


def test_an_accept_that_cites_the_brief_without_a_verified_scope_is_not_a_pass(tmp_path, capsys):
    argv, _ = _process_seat(tmp_path, f"{BRIEF_MED}\nTESTS: none: x\nVERDICT: ACCEPT\n",
                            PROCESS_PROMPT + PROCESS_DIFF)
    assert sv.main(argv + ["--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["body"]["ok"] is False


@pytest.mark.parametrize("prose", ["HIGH 12:30 UTC was the window", "HIGH-level coverage is fine"])
def test_prose_with_a_severity_word_or_timestamp_does_not_turn_an_accept_into_fix(tmp_path, capsys, prose):
    argv, _ = _process_seat(tmp_path, f"{prose}\nTESTS: none: x\nVERDICT: ACCEPT\n", PROCESS_PROMPT)
    assert sv.main(argv + ["--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["body"]["ok"] is True


def test_a_documented_finding_with_a_path_holding_a_space_still_blocks_an_accept(tmp_path, capsys):
    review = "HIGH docs/review notes.md:12 - missing test\nTESTS: none: x\nVERDICT: ACCEPT\n"
    argv, _ = _process_seat(tmp_path, review, PROCESS_PROMPT)
    assert sv.main(argv + ["--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["body"]["ok"] is False


def test_finding_citing_the_brief_only_in_its_advice_stays_a_real_finding(tmp_path, capsys):
    review = ("MED src/a.py:3 - the symbols callers are wrong; fix by editing review_brief.md\n"
              "VERDICT: REQUEST_CHANGES\n")
    argv, seat_dir = _process_seat(tmp_path, review, PROCESS_PROMPT + PROCESS_DIFF)
    assert sv.main(argv + ["--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["body"]["ok"] is False
    assert not (seat_dir / "process.json").exists()


def test_accept_with_a_counted_process_citation_is_not_posted_as_a_pass(tmp_path, capsys):
    changed = "diff --git a/src/plangraph/review/review_brief.md b/src/plangraph/review/review_brief.md\n+x\n"
    argv, _ = _process_seat(tmp_path, f"{BRIEF_MED}\nTESTS: none: x\nVERDICT: ACCEPT\n", PROCESS_PROMPT + changed)
    assert sv.main(argv + ["--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["body"]["ok"] is False


def test_clean_accept_with_severity_words_in_prose_is_posted_as_a_pass(tmp_path, capsys):
    review = "HIGH-level test coverage looks good.\nTESTS: none: x\nVERDICT: ACCEPT\n"
    argv, _ = _process_seat(tmp_path, review, PROCESS_PROMPT + PROCESS_DIFF)
    assert sv.main(argv + ["--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["body"]["ok"] is True


def test_a_saved_passing_retry_registers_the_pass_like_the_first_send(tmp_path, monkeypatch):
    import subprocess
    import time

    from plangraph.review import verdict as seats

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    head, binding = "c" * 40, {"head": "c" * 40, "key": "demo:A", "seat_run": "run-retry"}
    seats.record_hold(str(tmp_path), head, "run-held")
    identity = {"binding": binding, "cwd": str(tmp_path), "started": time.time() + 10}
    monkeypatch.setattr(sv, "check_record_permissions", lambda path: None)
    monkeypatch.setattr(sv, "read_verdict_payload", lambda d: {"payload": {"ok": True, "binding": binding}})
    monkeypatch.setattr(sv, "read_record_identity", lambda d, key=None: identity)
    monkeypatch.setattr(sv, "dispatch_verdict", lambda *args: 0)
    assert sv.send_saved_verdict(tmp_path, "demo:A", "http://x", tmp_path) == 0
    approval = f"RUN: run-retry\nHEAD: {head}\nVERDICT: PASS\n"
    assert seats.approval_hold_error(approval, head, str(tmp_path)) is None
