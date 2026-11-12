"""Offline seat-mode and accepted OpenCode configuration contracts."""
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import pytest

from plangraph.planner import legs
from plangraph.routing_support import review_mode
from plangraph.routing_support.clients import WINDOWS_ARGV_LIMIT, build_command, launch_stdin_payload
from plangraph.routing_support.review_mode import prompt_only_command
from tools.judge_prompt import build_judge_prompt


@pytest.mark.parametrize("mode", ["tools", "inline-only", "prompt-only"])
def test_judge_prompt_derived_from_leg(mode, monkeypatch):
    from plangraph.routing_support import clients

    leg = replace(legs.DEFAULT_LEGS[-1], model="claude/test", harness="claude", tool_mode=mode)
    monkeypatch.setattr(legs, "DEFAULT_LEGS", (leg,))
    captured = {}

    def capture(client, prompt, model, **kwargs):
        captured["prompt"] = prompt
        return []

    monkeypatch.setattr(clients, "build_standalone_fallback_command", capture)
    brief = build_judge_prompt("F1", "diff", {}, {}, tool_mode="tools")
    build_command(brief, leg.model, role="review", client=leg.harness)
    assert ("No tools are available" in captured["prompt"]) == (mode != "tools")
    assert ("Read/Grep/Glob" in captured["prompt"]) == (mode == "tools")


def test_prompt_only_accepted_config_schema(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", '{"providers":{"gateway":{}}}')
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    captured = {}

    def launch(cmd, *, env):
        path = Path(env["OPENCODE_CONFIG"])
        captured.update(config=json.loads(path.read_text()), path=path)
        assert "OPENCODE_CONFIG_CONTENT" not in env
        if os.name != "nt":
            assert path.stat().st_mode & 0o077 == 0
        assert cmd == ["opencode", "run", "--agent", "lean-review"]
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", launch)
    cmd = prompt_only_command(["opencode", "run"])
    assert review_mode.main(cmd[2:]) == 0
    config = captured["config"]
    assert not captured["path"].exists()
    schema = json.loads(Path("tests/fixtures/opencode-review-schema.json").read_text())
    jsonschema.Draft202012Validator(schema).validate(config)
    invalid = json.loads(json.dumps(config))
    invalid["agents"]["lean-review"]["permissions"] = {"*": "deny"}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(invalid, schema)


@pytest.mark.parametrize("gateway_online", [True, False])
def test_prompt_only_large_config_bounded_argv(monkeypatch, tmp_path, gateway_online):
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    config = {"providers": {"gateway": {"name": "x" * (WINDOWS_ARGV_LIMIT * 2)}}}
    monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", json.dumps(config))
    cmd = build_command("evidence", "gateway/test", client="opencode", role="review",
                        tool_mode="prompt-only", gateway_online=gateway_online)
    assert len(subprocess.list2cmdline(cmd)) < WINDOWS_ARGV_LIMIT
    assert cmd[0] == sys.executable
    assert cmd[-2:] == ["--agent", "lean-review"]
    assert launch_stdin_payload(cmd, "inlined evidence", cmd[0], is_nt=True) == b"inlined evidence"


@pytest.mark.parametrize("exit_code", [0, 7])
def test_prompt_only_launcher_stdin_config_and_cleanup(monkeypatch, tmp_path, exit_code):
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    config = {"providers": {"gateway": {"name": "x" * (WINDOWS_ARGV_LIMIT * 2)}},
              "agents": {"other": {"steps": 2}},
              "policies": [{"action": "provider.use", "resource": "gateway", "effect": "allow"}]}
    monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", json.dumps(config))
    script = (
        "import json, os, pathlib, sys; "
        "cfg = json.loads(pathlib.Path(os.environ['OPENCODE_CONFIG']).read_text()); "
        "assert 'OPENCODE_CONFIG_CONTENT' not in os.environ; "
        f"assert len(cfg['providers']['gateway']['name']) == {WINDOWS_ARGV_LIMIT * 2}; "
        "assert cfg['agents']['lean-review']['steps'] == 1; "
        "assert cfg['agents']['other']['steps'] == 2; "
        "assert cfg['policies'][0]['action'] == 'provider.use'; "
        "assert sys.stdin.read() == 'inlined evidence'; "
        "print('launcher-ran'); "
        f"sys.exit({exit_code})"
    )
    cmd = prompt_only_command([sys.executable, "-c", script])
    result = subprocess.run(cmd,
                            input="inlined evidence", capture_output=True, text=True)
    assert result.returncode == exit_code, result.stderr
    assert result.stdout == "launcher-ran\n"
    assert not list(tmp_path.iterdir())


def test_prompt_only_launcher_cleans_failed_spawn(monkeypatch, tmp_path):
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", '{"providers":{}}')
    cmd = prompt_only_command(["opencode", "run"])

    def fail(*args, **kwargs):
        raise FileNotFoundError("fixture executable missing")

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(FileNotFoundError, match="fixture executable missing"):
        review_mode.main(cmd[2:])
    assert not Path(cmd[2]).exists()


def test_jsonschema_declared_test_dependency():
    requirements = Path("requirements-ci.txt").read_text().splitlines()
    assert "jsonschema>=4" in requirements  # Draft 2020-12 support, not a runtime dependency.


def test_missing_config_preserves_discovery(monkeypatch):
    monkeypatch.delenv("OPENCODE_CONFIG_CONTENT", raising=False)
    cmd = build_command("p", "gateway/test", client="opencode", role="review", tool_mode="prompt-only")
    assert cmd == build_command("p", "gateway/test", client="opencode", role="review", tool_mode="tools")


@pytest.mark.parametrize("value", ["{bad", "null", "[]", '{"agents":[]}'])
def test_malformed_config_falls_back(monkeypatch, value):
    monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", value)
    cmd = ["opencode", "run", "--model", "unknown/pair"]
    assert prompt_only_command(cmd) == cmd
