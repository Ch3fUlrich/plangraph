"""tests/test_session_reap_mark.py - ttl.json marker is only ever written inside a session's own job dir (D-2442)."""

from __future__ import annotations

import json

import pytest

from plangraph.session_reap.mark import main as mark_main, mark_session


def test_mark_rejects_non_positive_ttl(tmp_path):
    job = tmp_path / "j"
    job.mkdir()
    for bad in (0, -1):
        with pytest.raises(ValueError):
            mark_session(ttl_min=bad, job_dir=job)
        with pytest.raises(SystemExit) as exc:
            mark_main(["--ttl-min", str(bad), "--dir", str(job)])
        assert exc.value.code != 0
    assert not (job / "ttl.json").exists()


def test_mark_resolves_to_nearest_ancestor_with_state_json(tmp_path, monkeypatch):
    job = tmp_path / "jobs" / "abc123"
    (job / "work" / "deep").mkdir(parents=True)
    (job / "state.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_JOB_DIR", str(job / "work" / "deep"))
    assert mark_main(["--ttl-min", "20"]) == 0
    assert json.loads((job / "ttl.json").read_text(encoding="utf-8")) == {"ttl_min": 20, "allow_stop": True}
    assert not (job.parent / "ttl.json").exists()


def test_mark_uses_job_dir_itself_when_it_holds_state_json(tmp_path, monkeypatch):
    job = tmp_path / "jobs" / "abc123"
    job.mkdir(parents=True)
    (job / "state.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_JOB_DIR", str(job))
    assert mark_main(["--ttl-min", "5", "--no-stop"]) == 0
    assert json.loads((job / "ttl.json").read_text(encoding="utf-8"))["allow_stop"] is False


def test_mark_refuses_when_no_state_json_anywhere_above(tmp_path, monkeypatch, capsys):
    jobs = tmp_path / "jobs"
    (jobs / "abc123" / "work").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_JOB_DIR", str(jobs / "abc123" / "work"))
    with pytest.raises(SystemExit) as exc:
        mark_main(["--ttl-min", "20"])
    assert exc.value.code != 0
    assert "state.json" in capsys.readouterr().err
    assert not list(tmp_path.rglob("ttl.json"))
