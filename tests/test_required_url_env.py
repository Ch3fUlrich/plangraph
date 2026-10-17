"""No default ports (D-A): PG_URL / PLANGRAPH_REVIEW_URL required, errors name the var."""
import io
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import apply_batch  # noqa: E402
import seat_verdict as sv  # noqa: E402

from plangraph.review import gateway as g  # noqa: E402

TOKEN = "dummy"


def test_gateway_call_requires_review_url():
    with pytest.raises(g.MissingGatewayURL) as exc:
        g.call("m", "p", env={"PLANGRAPH_REVIEW_KEY": "k"})
    assert g.GATEWAY_ENV_VAR in str(exc.value)


def test_gateway_uses_review_url_env():
    res = g.call("m", "p", env={"PLANGRAPH_REVIEW_KEY": "k",
                                g.GATEWAY_ENV_VAR: "http://127.0.0.1:1"})
    assert isinstance(res, g.CallResult) and res.status == 0


def test_gateway_no_default_gateway_constant():
    assert not hasattr(g, "DEFAULT_GATEWAY")


def _seat(tmp_path):
    d = tmp_path / "seat"
    d.mkdir()
    evidence = {"requested_model": "gpt-oss-120b", "served_model": "gpt-oss-120b",
                "finish_reason": "stop", "test_gaming": "no",
                "correlation_id": "c", "prompt_sha256": "ab" * 32}
    (d / "evidence.json").write_text(json.dumps(evidence))
    (d / "review.txt").write_text("VERDICT: FIX\nwhy")
    tokens = tmp_path / "tok"
    tokens.mkdir()
    (tokens / "reviewer-openai.token").write_text(TOKEN + "\n")
    return ["--key", "demo:A", "--seat-dir", str(d), "--writer-family", "meta",
            "--token-dir", str(tokens)]


def test_seat_verdict_requires_url(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("PG_URL", raising=False)
    assert sv.main(_seat(tmp_path)) == sv.EXIT_IO
    assert "PG_URL" in capsys.readouterr().err


def test_seat_verdict_dry_run_without_url(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("PG_URL", raising=False)
    argv = _seat(tmp_path) + ["--dry-run"]
    assert sv.main(argv) == 0
    out = capsys.readouterr().out
    assert TOKEN not in out
    assert json.loads(out)["body"]["ok"] is False


def test_apply_batch_requires_url(tmp_path, monkeypatch):
    monkeypatch.delenv("PG_URL", raising=False)
    out, err = io.StringIO(), io.StringIO()
    rc = apply_batch.main([str(tmp_path / "none.yaml"), "--token-file",
                           str(tmp_path / "none.token")], out=out, err=err)
    assert rc == 2
    assert "PG_URL" in err.getvalue()


def test_apply_batch_url_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PG_URL", "http://127.0.0.1:1")
    (tmp_path / "none.yaml").write_text("batch: b\nprincipal: router\n"
                                         "source: s\nsource_sha256: x\nops: []\n")
    out, err = io.StringIO(), io.StringIO()
    rc = apply_batch.main([str(tmp_path / "none.yaml"), "--token-file",
                           str(tmp_path / "none.token")], out=out, err=err)
    assert rc == 1  # no url error: fails later on the unreadable source instead
    assert "PG_URL" not in err.getvalue()
