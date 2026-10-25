"""Tests for Windows gateway provider render parity (plangraph:SPAWN-WIN-PARITY, S1).

Hermetic per I16 / D-852: no network, no real opencode, no real keys file.
Platform simulated via is_nt hooks, never monkeypatching os.name.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from unittest.mock import AsyncMock

import pytest

from plangraph.mimo_policy import ACCEPTED_MIMO_IDS
from plangraph.routing_support import host_keys
from plangraph.routing_support.clients import build_child_env
from plangraph.routing_support.opencode_overlay import overlay_env
from plangraph.routing_support.opencode_render import (
    REFUSAL_TOO_LARGE,
    REFUSAL_UNRESOLVED,
    render_gateway_providers,
)
from plangraph.spawner.runner import SpawnerRunner, spawn_service


@pytest.fixture(autouse=True)
def clean_gateway_env(monkeypatch):
    """Ensure tests start without ambient gateway endpoints or AutoOS root."""
    for var in (
        "AUTOOS_ROOT",
        "OMNIROUTE_BASE_URL",
        "PLANGRAPH_GATEWAY_URL",
        "AUTOOS_OMNIROUTE_URL",
        "AUTOOS_OMNIROUTE_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


def test_render_mimo_template_and_base_url_override(tmp_path, monkeypatch):
    """(a) No AutoOS root + OMNIROUTE_BASE_URL gives mimo provider with /v1 and env."""
    monkeypatch.setenv("AUTOOS_ROOT", str(tmp_path / "nonexistent-root"))
    env = {"OMNIROUTE_BASE_URL": "http://gw:20128"}
    reason = render_gateway_providers(env, "mimo/mimo-v2.6-pro")
    assert reason is None

    cfg = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    mimo = cfg["providers"]["mimo"]
    assert mimo["settings"]["baseURL"] == "http://gw:20128/v1"
    assert mimo["env"] == ["AUTOOS_OMNIROUTE_KEY"]
    assert mimo["package"] == "@opencode/ai/providers/openai-compatible"
    for mid in ACCEPTED_MIMO_IDS:
        short = mid.split("/", 1)[1]
        assert short in mimo["models"]
        assert mimo["models"][short]["modelID"] == mid
        assert mimo["models"][short]["limit"] == {"context": 1000000, "output": 65536}


def test_render_unresolved_base_url_fails_closed(tmp_path, monkeypatch):
    """(b) No root and no URL source gives refusal; process_factory never called."""
    monkeypatch.setenv("AUTOOS_ROOT", str(tmp_path / "absent-root"))
    factory = AsyncMock()
    runner = SpawnerRunner(process_factory=factory)
    service = spawn_service(runner, tmp_path)

    async def run():
        record = await service.spawn(
            card={"estimate_tokens": 1000},
            model="mimo/mimo-v2.6-pro",
            override_reason="testing manual model override",
            prompt="test",
            wait_seconds=0.1,
        )
        assert factory.call_count == 0
        assert record["status"] == "failed"
        assert "not_served_on_host" in (record.get("error") or "")

    asyncio.run(run())


def test_render_linux_root_fixture_parity(tmp_path, monkeypatch):
    """(c) AutoOS-root fixture + no env gives provider blocks equal to today's overlay (I7)."""
    fake_root = tmp_path / "fake-autoos"
    fake_root.mkdir()
    (fake_root / "opencode.jsonc").write_text(json.dumps({"providers": {"mimo": {
        "package": "@opencode/ai/providers/openai-compatible", "name": "MiMo direct gateway legs",
        "env": ["AUTOOS_OMNIROUTE_KEY"], "settings": {"baseURL": "http://127.0.0.1:20128/v1"},
        "models": {m.split("/", 1)[1]: {"modelID": m} for m in ACCEPTED_MIMO_IDS},
    }}}))
    monkeypatch.setenv("AUTOOS_ROOT", str(fake_root))

    child_env = build_child_env()
    overlaid_env = overlay_env(dict(child_env))
    rendered_env = dict(overlaid_env)
    reason = render_gateway_providers(rendered_env, "mimo/mimo-v2.6-pro")
    assert reason is None

    p_overlaid = json.loads(overlaid_env["OPENCODE_CONFIG_CONTENT"])["providers"]
    p_rendered = json.loads(rendered_env["OPENCODE_CONFIG_CONTENT"])["providers"]
    assert p_rendered == p_overlaid


def test_sentinel_key_never_leaks(tmp_path, monkeypatch):
    """(d) Sentinel key is absent from config, argv, files under tmp_path, and record."""
    sentinel = "FAKE-KEY-0123456789"
    monkeypatch.setenv("AUTOOS_OMNIROUTE_KEY", sentinel)
    monkeypatch.setenv("PLANGRAPH_GATEWAY_URL", "http://127.0.0.1:20128/v1")
    monkeypatch.setenv("AUTOOS_ROOT", str(tmp_path / "absent-root"))

    runner = SpawnerRunner()
    service = spawn_service(runner, tmp_path)

    async def run():
        record = await service.spawn(
            card={"estimate_tokens": 1000},
            model="mimo/mimo-v2.6-pro",
            override_reason="testing manual model override",
            prompt="task",
            dry_run=True,
        )
        job = list(runner.jobs.values())[-1]
        child_env = runner._child_envs.get(job.job_id, {})
        assert sentinel not in child_env.get("OPENCODE_CONFIG_CONTENT", "")
        assert sentinel not in " ".join(job.command)
        assert sentinel not in json.dumps(record)
        assert sentinel not in record.get("stdout_tail", "")
        assert sentinel not in record.get("stderr_tail", "")

        for dirpath, _, filenames in os.walk(tmp_path):
            for fn in filenames:
                assert sentinel not in (tmp_path / dirpath / fn).read_text(errors="ignore")

    asyncio.run(run())


def test_render_never_imports_host_keys_or_opens_api_keys(tmp_path, monkeypatch):
    """(e) Render module never imports host_keys and never calls _gateway_key."""
    monkeypatch.setattr(
        host_keys, "_gateway_key", lambda *a, **k: pytest.fail("render called _gateway_key")
    )
    monkeypatch.setattr(
        host_keys, "_zen_key", lambda *a, **k: pytest.fail("render called _zen_key")
    )
    env = {"OMNIROUTE_BASE_URL": "http://gw:20128"}
    reason = render_gateway_providers(env, "mimo/mimo-v2.6-pro")
    assert reason is None
    render_mod = sys.modules["plangraph.routing_support.opencode_render"]
    assert "host_keys" not in render_mod.__dict__


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://u:p@gw",
        "http://gw/?k=1",
        "file:///x",
        "http://gw#frag",
    ],
)
def test_render_url_validation_rejections(bad_url, tmp_path, monkeypatch):
    """(f) I5 rejections: userinfo, query, scheme, fragment."""
    monkeypatch.setenv("AUTOOS_ROOT", str(tmp_path / "absent-root"))
    env = {"OMNIROUTE_BASE_URL": bad_url}
    reason = render_gateway_providers(env, "mimo/mimo-v2.6-pro")
    assert reason == REFUSAL_UNRESOLVED


def test_render_oversized_config_on_nt():
    """(g) With is_nt=True and oversized content (>30000), result is config_too_large."""
    big_doc = {"providers": {"mimo": {"dummy": "x" * 31000}}}
    env = {
        "OPENCODE_CONFIG_CONTENT": json.dumps(big_doc),
        "PLANGRAPH_GATEWAY_URL": "http://127.0.0.1:20128/v1",
    }
    reason_nt = render_gateway_providers(env, "mimo/mimo-v2.6-pro", is_nt=True)
    assert reason_nt == REFUSAL_TOO_LARGE

    # Same content with is_nt=False succeeds on POSIX
    env["OPENCODE_CONFIG_CONTENT"] = json.dumps(big_doc)
    reason_posix = render_gateway_providers(env, "mimo/mimo-v2.6-pro", is_nt=False)
    assert reason_posix is None


def test_add_host_keys_receives_needs_gateway_for_mimo_without_root(tmp_path, monkeypatch):
    """(h) add_host_keys receives needs_gateway=True for mimo when there is no root."""
    monkeypatch.setenv("AUTOOS_ROOT", str(tmp_path / "absent-root"))
    monkeypatch.setenv("PLANGRAPH_GATEWAY_URL", "http://127.0.0.1:20128/v1")
    captured = {}
    orig_add_host_keys = host_keys.add_host_keys

    def spy_add_host_keys(env, model, *, needs_gateway):
        captured["needs_gateway"] = needs_gateway
        return orig_add_host_keys(env, model, needs_gateway=needs_gateway)

    monkeypatch.setattr(
        "plangraph.routing_support.host_keys.add_host_keys", spy_add_host_keys
    )
    runner = SpawnerRunner()

    async def run():
        await runner.spawn(
            prompt="test", model_or_combo="mimo/mimo-v2.6-pro", dry_run=True
        )
        assert captured.get("needs_gateway") is True

    asyncio.run(run())


def test_parent_env_gateway_key_passthrough_and_no_leak(tmp_path, monkeypatch):
    """(i) Router D-2801 (a): parent AUTOOS_OMNIROUTE_KEY reaches child env without leaking."""
    sentinel = "SENTINEL-PARENT-ENV-KEY-999"
    monkeypatch.setenv("AUTOOS_OMNIROUTE_KEY", sentinel)
    monkeypatch.setenv("PLANGRAPH_GATEWAY_URL", "http://127.0.0.1:20128/v1")
    monkeypatch.setenv("AUTOOS_ROOT", str(tmp_path / "absent-root"))

    captured_env = {}

    async def fake_factory(exe, *args, **kwargs):
        captured_env.update(kwargs.get("env") or {})
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"ok", b""))
        proc.returncode = 0
        proc.pid = 999
        return proc

    runner = SpawnerRunner(process_factory=fake_factory)
    service = spawn_service(runner, tmp_path)

    async def run():
        record = await service.spawn(
            card={"estimate_tokens": 1000},
            model="mimo/mimo-v2.6-pro",
            override_reason="testing manual model override",
            prompt="task",
            wait_seconds=None,
        )
        job = list(runner.jobs.values())[-1]
        assert captured_env.get("AUTOOS_OMNIROUTE_KEY") == sentinel
        assert sentinel not in captured_env.get("OPENCODE_CONFIG_CONTENT", "")
        assert sentinel not in " ".join(job.command)
        assert sentinel not in json.dumps(record)
        assert sentinel not in record.get("stdout_tail", "")
        assert sentinel not in record.get("stderr_tail", "")
        record_file = service._path(record["run_id"])
        if record_file.exists():
            assert sentinel not in record_file.read_text()

    asyncio.run(run())


def test_render_null_env_treated_as_absent():
    """F2 (H5): a provider with "env": null never raises and counts as absent."""
    env = {
        "OMNIROUTE_BASE_URL": "http://gw:20128",
        "OPENCODE_CONFIG_CONTENT": json.dumps({"providers": {
            "mimo": {"env": None, "settings": {}},
            "omniroute": {"env": {"weird": 1}, "settings": {}},
            "custom": {"env": 7, "settings": {}},
        }}),
    }
    reason = render_gateway_providers(env, "mimo/mimo-v2.6-pro")
    assert reason is None
    cfg = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    # Gateway providers with a non-list env get the gateway key name filled in.
    assert cfg["providers"]["mimo"]["env"] == ["AUTOOS_OMNIROUTE_KEY"]
    assert cfg["providers"]["omniroute"]["env"] == ["AUTOOS_OMNIROUTE_KEY"]
    assert cfg["providers"]["mimo"]["settings"]["baseURL"] == "http://gw:20128/v1"
    assert cfg["providers"]["omniroute"]["settings"]["baseURL"] == "http://gw:20128/v1"
    # A non-gateway provider is left untouched.
    assert cfg["providers"]["custom"]["env"] == 7


def test_render_non_gateway_model_with_null_env_no_raise():
    """F2: the model-prefix gate itself must survive a non-list env block."""
    env = {
        "OMNIROUTE_BASE_URL": "http://gw:20128",
        "OPENCODE_CONFIG_CONTENT": json.dumps({"providers": {"custom": {"env": None}}}),
    }
    assert render_gateway_providers(env, "custom/thing") is None


def test_redact_env_values_null_env_provider_still_redacts():
    """F2 sweep: a non-list env in one provider must not abort value redaction."""
    from plangraph.spawner.env_redact import redact_env_values
    env = {
        "PLAIN_NAME": "sentinel-value-1234",
        "OPENCODE_CONFIG_CONTENT": json.dumps({"providers": {
            "a": {"env": None},
            "b": {"env": ["PLAIN_NAME"]},
        }}),
    }
    assert "sentinel-value-1234" not in redact_env_values("x sentinel-value-1234 y", env)


def test_add_host_keys_not_called_on_refusal(tmp_path, monkeypatch):
    """F3: a refused launch never reads host keys and never parks the child env."""
    monkeypatch.setenv("AUTOOS_ROOT", str(tmp_path / "absent-root"))
    calls = []
    monkeypatch.setattr(host_keys, "add_host_keys", lambda *a, **k: calls.append(k))
    factory = AsyncMock()
    runner = SpawnerRunner(process_factory=factory)

    async def run():
        job = await runner.spawn(prompt="test", model_or_combo="mimo/mimo-v2.6-pro")
        assert job.status == "failed"
        assert "not_served_on_host" in job.stderr
        assert calls == []
        assert factory.call_count == 0
        assert runner._child_envs == {}

    asyncio.run(run())
