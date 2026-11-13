"""S01 / I1: shared registry data, strict reasons and stable compatibility exports."""
import hashlib
import importlib.util
import json
import os
from importlib.resources import files
from pathlib import Path
import sys
import tomllib

import pytest

from _prompt_transport import transport_sandbox
from plangraph.routing_support import clients, review_mode

REASONS_SHA256 = "b474026a839d53dfe4c46ef7c9e585cda80a9975ea843977e0d0c3794bba1c56"


@pytest.mark.parametrize("error", [FileNotFoundError, ValueError])
def test_client_import_does_not_read_contract(monkeypatch, error):
    def unreadable(*args, **kwargs):
        raise error("prompt transport resource unavailable")

    monkeypatch.setattr("importlib.resources.files", unreadable)
    monkeypatch.setattr(review_mode, "files", unreadable)
    spec = importlib.util.spec_from_file_location("_transport_import_probe", clients.__file__)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    assert module.STDIN_PROMPT_HARNESSES == clients.STDIN_PROMPT_HARNESSES


@pytest.mark.parametrize("name", ["PATH", "SYSTEMROOT", "COMSPEC", "PATHEXT", "TEMP", "TMP"])
def test_sandbox_preserves_platform_env(monkeypatch, tmp_path, name):
    monkeypatch.setenv(name, str(tmp_path))
    monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", "{}")
    transport_sandbox.__wrapped__(monkeypatch, tmp_path)
    assert os.environ[name] == str(tmp_path)
    assert "OPENCODE_CONFIG_CONTENT" not in os.environ


def test_packaged_contract_resource():
    resource = files("plangraph").joinpath("config/prompt_transport_v1.json")
    assert resource.is_file()
    assert json.loads(resource.read_text(encoding="utf-8"))["version"] == 1
    packaging = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    assert "config/*.json" in packaging["tool"]["setuptools"]["package-data"]["plangraph"]


def test_registry_and_compat_exports(transport_sandbox):
    from plangraph.spawner import clients as legacy_clients

    fixture = transport_sandbox
    contract = review_mode.load_prompt_transport_contract()
    assert review_mode.load_prompt_transport_contract(json.dumps(contract)) == contract
    assert list(contract["reasons"]) == fixture["reason_ids"]
    assert contract["metadata"]["model_utf8_bytes"] == 256
    assert contract["metadata"]["title_ascii_bytes"] == 22
    # Pin every D2 spelling without maintaining a second reason-name table.
    spellings = "|".join(contract["reasons"].values()).encode()
    assert hashlib.sha256(spellings).hexdigest() == REASONS_SHA256
    assert clients.STDIN_PROMPT_HARNESSES == tuple(row["harness"] for row in fixture["modes"])
    assert clients.STDIN_PROMPT_HARNESSES == tuple(row["harness"] for row in contract["modes"])
    task = fixture["task"]["unit"] * fixture["task"]["repeat"]
    assert len(task.encode("utf-8")) == fixture["task"]["utf8_bytes"] == 307200
    for expected, mode in zip(fixture["modes"], contract["modes"], strict=True):
        assert expected["stdin"] == mode["stdin"]
        assert mode["evidence"] == "contract-only"
        if "alias" in expected:
            assert expected["alias"] in mode["aliases"]
        argv = expected["argv"]
        payload = clients.launch_stdin_payload(argv, task, argv[0])
        if mode["stdin"] == "ndjson":
            assert payload.endswith(b"\n") and len(payload.splitlines()) == 1
            assert json.loads(payload)["message"]["content"] == task
        else:
            assert payload == task.encode("utf-8")
    for reason_id, name in contract["reasons"].items():
        assert review_mode.prompt_transport_reason(reason_id) == name
        assert review_mode.prompt_transport_reason_id(name) == reason_id
    for name in ("launch_stdin_payload", "build_command", "without_prompt_env"):
        assert callable(getattr(clients, name))
        assert getattr(legacy_clients, name) is getattr(clients, name)
    assert legacy_clients.PromptTransportError is clients.PromptTransportError
    assert issubclass(clients.PromptTransportError, RuntimeError)
    assert clients.WINDOWS_ARGV_LIMIT == contract["capacity"]["windows_native_units"]
    assert clients.WINDOWS_BATCH_ARGV_LIMIT == contract["capacity"]["windows_batch_units"]
    assert callable(review_mode.is_prompt_only_command)
    peer = review_mode.load_prompt_transport_contract()
    contract["modes"][0]["aliases"].append("test-mutation")
    assert peer["modes"][0]["aliases"] == []
    assert review_mode.load_prompt_transport_contract()["modes"][0]["aliases"] == []


@pytest.mark.parametrize("lookup,value", [("prompt_transport_reason", "unknown"),
                                        ("prompt_transport_reason_id", "unknown")])
def test_unknown_reason_refused(transport_sandbox, lookup, value):
    with pytest.raises(ValueError, match="unknown prompt transport reason"):
        getattr(review_mode, lookup)(value)


@pytest.mark.parametrize("mutation", [
    "duplicate_key", "duplicate_name", "unknown_id", "unknown_name", "duplicate_alias",
    "modes", "capacity", "flags", "metadata", "status", "grammar", "flag_type",
])
def test_invalid_registry_refused(transport_sandbox, mutation):
    contract = review_mode.load_prompt_transport_contract()
    if mutation == "duplicate_name":
        contract["reasons"]["C"] = contract["reasons"]["P"]
    elif mutation == "unknown_id":
        contract["reasons"]["unknown"] = contract["reasons"].pop("C")
    elif mutation == "unknown_name":
        contract["reasons"]["C"] = "unknown"
    elif mutation == "duplicate_alias":
        contract["modes"][0]["aliases"].append(contract["modes"][1]["harness"])
    elif mutation == "modes":
        contract["modes"][0]["stdin"] = "fabricated"
    elif mutation == "capacity":
        contract["capacity"]["windows_native_units"] += 1
    elif mutation == "flags":
        contract["flags"]["--model"]["arity"] = 0
    elif mutation == "flag_type":
        contract["flags"]["--model"]["arity"] = True
    elif mutation in contract:
        contract[mutation]["fabricated"] = True
    raw = json.dumps(contract)
    if mutation == "duplicate_key":
        raw = '{"version": 1, ' + raw[1:]
        assert json.loads(raw) == contract
    with pytest.raises(ValueError, match="duplicate.*key" if mutation == "duplicate_key" else None):
        review_mode.load_prompt_transport_contract(raw)
