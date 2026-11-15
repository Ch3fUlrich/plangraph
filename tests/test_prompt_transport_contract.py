"""S01 / I1: shared registry data, strict reasons and stable compatibility exports."""
import hashlib
import json
import os
from importlib.resources import files
from pathlib import Path
import subprocess
import sys
import tomllib
from unittest.mock import Mock

import pytest

from _prompt_transport import FIXTURES, transport_sandbox
from plangraph.routing_support import clients, review_mode

REASONS_SHA256 = "b474026a839d53dfe4c46ef7c9e585cda80a9975ea843977e0d0c3794bba1c56"


@pytest.mark.parametrize("error", [FileNotFoundError, ValueError])
def test_client_import_does_not_read_contract(error):
    probe = f'''
from importlib import resources
from unittest.mock import Mock, patch
unreadable = Mock(side_effect={error.__name__}("contract unavailable"))
with patch.object(resources, "files", unreadable):
    from plangraph.routing_support import review_mode
    with patch.object(review_mode, "load_prompt_transport_contract", unreadable):
        from plangraph.routing_support import clients
assert unreadable.call_count == 0
try:
    review_mode.load_prompt_transport_contract()
except {error.__name__}:
    pass
else:
    raise AssertionError("contract failure was not deferred until first use")
assert unreadable.call_count == 1
'''
    # conftest's sys.path bootstrap is not inherited by a fresh interpreter.
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    subprocess.run([sys.executable, "-c", probe], env=env,
                   check=True, capture_output=True, text=True)


def test_sandbox_preserves_platform_env(monkeypatch, tmp_path, request):
    kept = dict.fromkeys(("PATH", "SystemRoot", "SYSTEMROOT", "COMSPEC", "PATHEXT", "TEMP", "TMP"), str(tmp_path))
    polluted = dict.fromkeys(("OPENCODE_CONFIG_CONTENT", "OPENCODE_CONFIG", "PLANGRAPH_GATEWAY_URL",
                             "OMNIROUTE_BASE_URL", "PROMPT", "PROMPT_TEXT", "PROMPT_SUFFIX",
                             "TASK_PROMPT", "PLANGRAPH_TASK_PROMPT", "UNRELATED"), "polluted")
    monkeypatch.setattr(os, "environ", {**kept, **polluted})
    request.getfixturevalue("transport_sandbox")
    homes = {name: str(tmp_path / name.lower()) for name in
             ("HOME", "USERPROFILE", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME")}
    assert dict(os.environ) == {**kept, **homes,
                               "PLANGRAPH_ROUTE_POLICY": str(FIXTURES / "prompt_transport_policy.json")}


def test_packaged_contract_resource():
    resource = files("plangraph").joinpath("config/prompt_transport_v1.json")
    assert resource.is_file()
    assert json.loads(resource.read_text(encoding="utf-8"))["version"] == 1
    packaging = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    assert "config/*.json" in packaging["tool"]["setuptools"]["package-data"]["plangraph"]


def test_registry_and_compat_exports(transport_sandbox, monkeypatch):
    from plangraph.spawner import clients as legacy_clients

    fixture = transport_sandbox
    review_mode._packaged_contract.cache_clear()
    monkeypatch.setattr(review_mode, "files", Mock(wraps=review_mode.files))
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
    assert contract["modes"][1]["forbidden_flags"] == ["--bg"]  # Staged data, not launch enforcement.
    assert callable(review_mode.is_prompt_only_command)
    peer = review_mode.load_prompt_transport_contract()
    contract["modes"][0]["aliases"].append("test-mutation")
    assert peer["modes"][0]["aliases"] == []
    assert review_mode.load_prompt_transport_contract()["modes"][0]["aliases"] == []
    assert review_mode.files.call_count == 1
    review_mode._packaged_contract.cache_clear()
    assert review_mode.prompt_transport_reason("P") == contract["reasons"]["P"]
    assert review_mode.files.call_count == 2
    review_mode._packaged_contract.cache_clear()


@pytest.mark.parametrize("lookup,value", [("prompt_transport_reason", "unknown"),
                                        ("prompt_transport_reason_id", "unknown")])
def test_unknown_reason_refused(transport_sandbox, lookup, value):
    with pytest.raises(ValueError, match="unknown prompt transport reason"):
        getattr(review_mode, lookup)(value)


@pytest.mark.parametrize("mutation,error", [
    ("duplicate_key", "duplicate prompt transport contract key"),
    ("duplicate_name", "duplicate prompt transport reason name"),
    ("unknown_id", "invalid prompt transport reason IDs"),
    ("unknown_name", "unknown prompt transport reason name"),
    ("duplicate_harness", "invalid or duplicate prompt transport harness"),
    ("duplicate_alias", "invalid or duplicate prompt transport alias"),
    *[(part, "unknown prompt transport contract data") for part in
      ("modes", "capacity", "flags", "metadata", "status", "grammar", "flag_type", "forbidden_flags")],
])
def test_invalid_registry_refused(transport_sandbox, mutation, error):
    contract = review_mode.load_prompt_transport_contract()
    if mutation == "duplicate_name":
        contract["reasons"]["C"] = contract["reasons"]["P"]
    elif mutation == "unknown_id":
        contract["reasons"]["unknown"] = contract["reasons"].pop("C")
    elif mutation == "unknown_name":
        contract["reasons"]["C"] = "unknown"
    elif mutation == "duplicate_harness":
        contract["modes"][1]["harness"] = contract["modes"][0]["harness"]
    elif mutation == "duplicate_alias":
        contract["modes"][1]["aliases"].append(contract["modes"][0]["harness"])
    elif mutation == "forbidden_flags":
        contract["modes"][1]["forbidden_flags"] = []
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
    with pytest.raises(ValueError, match=f"^{error}$"):
        review_mode.load_prompt_transport_contract(raw)
