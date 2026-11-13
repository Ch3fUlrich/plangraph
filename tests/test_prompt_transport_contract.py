"""S01 / I1: shared registry data, strict reasons and stable compatibility exports."""
import hashlib
import json
from pathlib import Path

import pytest

from _prompt_transport import transport_sandbox
from plangraph.routing_support import clients, review_mode

REASONS_SHA256 = "b474026a839d53dfe4c46ef7c9e585cda80a9975ea843977e0d0c3794bba1c56"


def test_registry_and_compat_exports(transport_sandbox):
    from plangraph.spawner import clients as legacy_clients

    fixture = transport_sandbox
    contract = review_mode.load_prompt_transport_contract()
    path = Path(__file__).resolve().parents[1] / "src/plangraph/config/prompt_transport_v1.json"
    assert contract == json.loads(path.read_text(encoding="utf-8"))
    assert list(contract["reasons"]) == fixture["reason_ids"]
    assert contract["metadata"]["model_utf8_bytes"] == 256
    assert contract["metadata"]["title_ascii_bytes"] == 22
    # Pin every D2 spelling without maintaining a second reason-name table.
    assert hashlib.sha256("|".join(contract["reasons"].values()).encode()).hexdigest() == REASONS_SHA256
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
    assert (clients.WINDOWS_ARGV_LIMIT, clients.WINDOWS_BATCH_ARGV_LIMIT) == (32000, 8000)
    assert callable(review_mode.is_prompt_only_command)
    contract["modes"][0]["aliases"].append("test-mutation")
    assert review_mode.load_prompt_transport_contract()["modes"][0]["aliases"] == []


@pytest.mark.parametrize("lookup,value", [("prompt_transport_reason", "unknown"),
                                        ("prompt_transport_reason_id", "unknown")])
def test_unknown_reason_refused(transport_sandbox, lookup, value):
    with pytest.raises(ValueError, match="unknown prompt transport reason"):
        getattr(review_mode, lookup)(value)


@pytest.mark.parametrize("mutation", ["duplicate_key", "duplicate_name", "unknown_id", "unknown_name", "duplicate_alias"])
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
    raw = json.dumps(contract)
    if mutation == "duplicate_key":
        raw = raw.replace('"reasons": {', '"reasons": {"P": "duplicate",', 1)
    with pytest.raises(ValueError):
        review_mode.load_prompt_transport_contract(raw)
