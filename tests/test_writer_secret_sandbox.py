"""Tests for writer secret sandbox and stream redaction (fake values only)."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from plangraph.spawner import resolver
from plangraph.spawner.runner import SpawnerRunner, redact
from plangraph.spawner.service import SpawnService
from plangraph.spawner.stream_redact import scrub_secrets
from plangraph.spawner.writer_sandbox import WRITER_DENY_PATTERNS, merge_writer_permissions

FAKE_DSN = "postgres://fakeuser:fakepw123@db.example:5432/x"


def test_scrub_secrets_url_and_key_values():
    assert "fakepw123" not in scrub_secrets(FAKE_DSN)
    assert "postgres://fakeuser:[autoos:redacted]@db.example:5432/x" in scrub_secrets(FAKE_DSN)

    kv_lines = (
        "PG_DSN=" + FAKE_DSN + "\n"
        "API_KEY=fake_secret_key\n"
        "DATABASE_PASSWORD: fakepassword123\n"
        '{"SECRET_TOKEN": "fakesecret", "other": "ok"}\n'
    )
    scrubbed = scrub_secrets(kv_lines)
    assert "fakepw123" not in scrubbed
    assert "fake_secret_key" not in scrubbed
    assert "fakepassword123" not in scrubbed
    assert "fakesecret" not in scrubbed
    assert "PG_DSN=[autoos:redacted]" in scrubbed
    assert "API_KEY=[autoos:redacted]" in scrubbed
    assert "DATABASE_PASSWORD: [autoos:redacted]" in scrubbed
    assert '"SECRET_TOKEN": "[autoos:redacted]"' in scrubbed
    assert '"other": "ok"' in scrubbed


def _run_spawn(tmp_path, monkeypatch, *, role="implement", stdout=b"", stderr=b""):
    async def probe(*args, **kwargs):
        return None
    monkeypatch.setattr(resolver, "host_skip_reason_async", probe)
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    seen = {}

    async def factory(*args, **kwargs):
        seen["env"] = dict(kwargs["env"])
        async def communicate():
            return stdout, stderr
        return SimpleNamespace(pid=123, returncode=0, communicate=communicate)

    runner = SpawnerRunner(default_cwd=str(work), process_factory=factory)

    async def run():
        job = await runner.spawn("prompt", model_or_combo="omniroute/cx-gpt-6.1-sol", role=role,
                                 client="opencode", cwd=str(work), wait_seconds=3)
        await runner._tasks[job.job_id]
        return job

    return asyncio.run(run()), runner, seen.get("env", {})


def test_redaction_scrubs_stdout_stderr_and_live_log(tmp_path, monkeypatch):
    stdout_raw = f"connected to {FAKE_DSN} ok\n".encode("utf-8")
    stderr_raw = f"PG_DSN={FAKE_DSN}\n".encode("utf-8")
    job, runner, _ = _run_spawn(tmp_path, monkeypatch, stdout=stdout_raw, stderr=stderr_raw)

    assert "fakepw123" not in job.stdout
    assert "postgres://fakeuser:[autoos:redacted]@db.example:5432/x" in job.stdout
    assert "fakepw123" not in job.stderr
    assert "PG_DSN=[autoos:redacted]" in job.stderr

    from plangraph.spawner.run_watchdog import LiveLog
    live_log_path = tmp_path / "live.log"
    dummy_job = SimpleNamespace(_live_log=str(live_log_path), events=[])
    log = LiveLog(dummy_job)
    log.feed(redact(f"connecting to {FAKE_DSN}\nPG_DSN={FAKE_DSN}\n"))
    log.flush()
    content = live_log_path.read_text()
    assert "fakepw123" not in content
    assert "postgres://fakeuser:[autoos:redacted]@db.example:5432/x" in content
    assert "PG_DSN=[autoos:redacted]" in content


def test_redaction_scrubs_run_record_and_provider_payload(tmp_path):
    service = SpawnService(state=tmp_path / "state")
    record = {
        "run_id": "run-test1234",
        "job_id": "job-test1234",
        "stdout_tail": f"info: {FAKE_DSN}",
        "stderr_tail": f"SECRET_KEY=fakekey999",
    }
    service._save(record)
    loaded = service.result("run-test1234")
    assert "fakepw123" not in loaded["stdout_tail"]
    assert "[autoos:redacted]" in loaded["stdout_tail"]
    assert "fakekey999" not in loaded["stderr_tail"]
    assert "[autoos:redacted]" in loaded["stderr_tail"]

    provider_payload = redact({
        "status": 429,
        "body": f"Quota exceeded for {FAKE_DSN}",
        "headers": {"authorization": "Bearer fakesecrettoken"},
    })
    assert "fakepw123" not in str(provider_payload)
    assert "[autoos:redacted]" in provider_payload["body"]


def test_writer_permission_config_denies_paths(tmp_path, monkeypatch):
    for role in ("implement", "fix"):
        job, _, env = _run_spawn(tmp_path, monkeypatch, role=role, stdout=b"done", stderr=b"")
        cfg = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        perm = cfg["permission"]

        for tool in ("read", "bash"):
            assert perm[tool]["*/proc/*/environ*"] == "deny"
            assert perm[tool]["*/.config/plangraph*"] == "deny"
            assert perm[tool]["*keeper.db*"] == "deny"

        assert perm["external_directory"] == "deny"

    # Preserves existing allows
    existing = {"permission": {"read": {"*.md": "allow"}, "bash": {"git*": "allow"}}}
    merged = merge_writer_permissions(existing)
    assert merged["read"]["*.md"] == "allow"
    assert merged["bash"]["git*"] == "allow"
    assert merged["read"]["*/proc/*/environ*"] == "deny"
    assert merged["bash"]["*/proc/*/environ*"] == "deny"


def test_review_role_config_unchanged(tmp_path, monkeypatch):
    job, _, env = _run_spawn(tmp_path, monkeypatch, role="review", stdout=b"done", stderr=b"")
    cfg = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    perm = cfg["permission"]
    # Review role continues to deny every tool via '*' mapping
    assert perm["*"] == "deny"
    assert perm["read"] == "deny"
    assert perm["bash"] == "deny"
