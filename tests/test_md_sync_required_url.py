"""PG-REPUBLISH-FRESH: md_sync / md_sync_archive require PG_URL or --url at call time.

apply_batch no longer snapshots PG_URL at import; the sync tools resolve the keeper
URL from ``--url`` / ``PG_URL`` when they run, and refuse with a clear stderr message
and exit 2 BEFORE any file read or network call when it is still empty.
"""
import io
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import apply_batch  # noqa: E402
import md_sync  # noqa: E402
import md_sync_archive as arch  # noqa: E402

NO_URL = "no keeper URL"


def _import_argv(tmp_path):
    """Required flags for the markdown-import path, pointing at files that must NOT be read."""
    return ["--questions", str(tmp_path / "missing-questions.md"),
            "--answers", str(tmp_path / "missing-answers.md"),
            "--decisions", str(tmp_path / "missing-decisions.md"),
            "--token-file", str(tmp_path / "missing.token")]


def test_apply_batch_has_no_import_time_url_default():
    assert not hasattr(apply_batch, "DEFAULT_URL")


def test_md_sync_import_requires_url_before_any_io(tmp_path, monkeypatch):
    monkeypatch.delenv("PG_URL", raising=False)
    out, err = io.StringIO(), io.StringIO()
    rc = md_sync.main(_import_argv(tmp_path), out=out, err=err)
    assert rc == 2
    text = err.getvalue()
    assert NO_URL in text and "PG_URL" in text and "--url" in text
    # refuse before opening files: no 'cannot read source/token' noise
    assert "cannot read" not in text


def test_md_sync_import_records_status_on_missing_url(tmp_path, monkeypatch):
    monkeypatch.delenv("PG_URL", raising=False)
    status = tmp_path / "status.json"
    out, err = io.StringIO(), io.StringIO()
    rc = md_sync.main(_import_argv(tmp_path) + ["--status-file", str(status)],
                      out=out, err=err)
    assert rc == 2 and status.exists()


def test_md_sync_archive_requires_url_before_any_io(tmp_path, monkeypatch):
    monkeypatch.delenv("PG_URL", raising=False)
    out, err = io.StringIO(), io.StringIO()
    rc = md_sync.main(["--archive-answers",
                       "--questions-out", str(tmp_path / "missing-QUESTIONS.md"),
                       "--token-file", str(tmp_path / "missing.token")],
                      out=out, err=err)
    assert rc == 2
    assert NO_URL in err.getvalue() and "PG_URL" in err.getvalue()
    assert "cannot read" not in err.getvalue()


def _raise_unreachable(self, verb):
    raise apply_batch.ApiError("unreachable", "URLError")


def _make_inputs(tmp_path):
    for name in ("questions.md", "answers.md", "decisions.md"):
        (tmp_path / name).write_text("", encoding="utf-8")
    (tmp_path / "tok").write_text("dummytoken", encoding="utf-8")


def test_md_sync_import_url_from_flag_still_works(tmp_path, monkeypatch):
    monkeypatch.delenv("PG_URL", raising=False)
    _make_inputs(tmp_path)
    monkeypatch.setattr(apply_batch.Session, "get", _raise_unreachable)
    out, err = io.StringIO(), io.StringIO()
    rc = md_sync.main(["--questions", str(tmp_path / "questions.md"),
                       "--answers", str(tmp_path / "answers.md"),
                       "--decisions", str(tmp_path / "decisions.md"),
                       "--token-file", str(tmp_path / "tok"),
                       "--url", "http://127.0.0.1:1"], out=out, err=err)
    assert NO_URL not in err.getvalue()
    assert rc == 2 and "cannot read the live graph" in err.getvalue()


def test_md_sync_import_url_from_env_still_works(tmp_path, monkeypatch):
    monkeypatch.setenv("PG_URL", "http://127.0.0.1:1")
    _make_inputs(tmp_path)
    monkeypatch.setattr(apply_batch.Session, "get", _raise_unreachable)
    out, err = io.StringIO(), io.StringIO()
    rc = md_sync.main(["--questions", str(tmp_path / "questions.md"),
                       "--answers", str(tmp_path / "answers.md"),
                       "--decisions", str(tmp_path / "decisions.md"),
                       "--token-file", str(tmp_path / "tok")], out=out, err=err)
    assert NO_URL not in err.getvalue()
    assert rc == 2 and "cannot read the live graph" in err.getvalue()


def test_md_sync_archive_url_from_flag_still_works(tmp_path, monkeypatch):
    monkeypatch.delenv("PG_URL", raising=False)
    (tmp_path / "tok").write_text("dummytoken", encoding="utf-8")
    monkeypatch.setattr(apply_batch.Session, "get", _raise_unreachable)
    out, err = io.StringIO(), io.StringIO()
    rc = md_sync.main(["--archive-answers",
                       "--questions-out", str(tmp_path / "QUESTIONS.md"),
                       "--token-file", str(tmp_path / "tok"),
                       "--url", "http://127.0.0.1:1"], out=out, err=err)
    assert NO_URL not in err.getvalue()
    assert rc == 2 and "cannot read the live graph" in err.getvalue()


def _fake_snapshot(self, verb):
    return {"items": [], "aliases": {"old:G": "new:G"}}


def test_md_sync_gate_flag_and_snapshot_aliases_reach_build_plan(tmp_path, monkeypatch):
    monkeypatch.delenv("PG_URL", raising=False)
    _make_inputs(tmp_path)
    monkeypatch.setattr(apply_batch.Session, "get", _fake_snapshot)
    captured = {}

    def fake_build(questions, answers, skipped, decision_ids, items, **kw):
        captured.update(kw)
        return md_sync.mplan.Plan()

    monkeypatch.setattr(md_sync.mplan, "build_plan", fake_build)
    out, err = io.StringIO(), io.StringIO()
    rc = md_sync.main(["--questions", str(tmp_path / "questions.md"),
                       "--answers", str(tmp_path / "answers.md"),
                       "--decisions", str(tmp_path / "decisions.md"),
                       "--token-file", str(tmp_path / "tok"),
                       "--url", "http://127.0.0.1:1",
                       "--gate", "plangraph:PH-OPERATOR"], out=out, err=err)
    assert rc == 0
    assert captured["gate"] == "plangraph:PH-OPERATOR"
    assert captured["aliases"] == {"old:G": "new:G"}
