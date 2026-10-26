"""Contract 0: the machine-readable contract file and the validator itself."""
import json
import re
from pathlib import Path

import pytest

import contract_schema as cs
from contract_world import err

DOC = Path(__file__).resolve().parents[2] / "docs" / "contract"


def _refs(node):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref":
                yield value
            else:
                yield from _refs(value)
    elif isinstance(node, list):
        for value in node:
            yield from _refs(value)


def test_contract_file_is_self_consistent():
    contract = cs.CONTRACT
    assert contract["contract_version"] == "1.0"
    assert contract["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    defs = contract["$defs"]
    for ref in _refs(defs):
        assert ref.rsplit("/", 1)[1] in defs, ref
    seen = set()
    for route in contract["routes"]:
        assert route["method"] in ("GET", "POST")
        assert route["path"].startswith("/v1/") and (route["method"], route["path"]) not in seen
        seen.add((route["method"], route["path"]))
        for field in ("request", "response"):
            if field in route:
                assert route[field] in defs, (route["path"], field)
    assert len(seen) == 44


def test_markdown_documents_every_route():
    text = (DOC / "keeper-contract-v1.md").read_text() + (DOC / "keeper-contract-v1-routes.md").read_text()
    for route in cs.CONTRACT["routes"]:
        assert f"`{route['method']} {route['path']}`" in text, route["path"]
    assert re.search(r"^# .*contract.*1\.0", text, re.I | re.M)


def test_every_declared_route_exists_and_demands_a_token(kp):
    for route in cs.CONTRACT["routes"]:
        resp = kp.call(route["method"], route["path"], None, body={} if route["method"] == "POST" else None)
        if route["roles"] == "none":
            assert resp.status == 200, route["path"]
        else:
            err(resp, 401, "forbidden")
    err(kp.get("/v1/not-a-route"), 404, "not_found")


def test_every_declared_route_rejects_the_wrong_method(kp):
    for route in cs.CONTRACT["routes"]:
        if route["roles"] == "none":
            continue
        other = "POST" if route["method"] == "GET" else "GET"
        resp = kp.call(other, route["path"], "router", body={} if other == "POST" else None)
        err(resp, 405, "method_not_allowed")


GOOD = {
    "Error": {"error": "forbidden", "msg": "x"},
    "WhoAmI": {"name": "router", "role": "router", "projects": ["demo"], "family": None,
               "features": ["apply_ops.dry_run", "apply_ops.strict_body"]},
    "Claim": {"key": "demo:A", "epoch": 1, "lease_until": "2030-01-01T00:00:00Z"},
    "Blocked": {"demo:A": ["demo:B"]},
    "Waves": [["demo:A"], ["demo:B"]],
    "Percent": 12.5,
}
BAD = {
    "Error": [{"error": "nope", "msg": "x"}, {"error": "forbidden"}, "text", None],
    # qwen-R6: ``features`` is pinned on the wire, so the contract must register
    # it -- a response missing it, shaped wrongly, naming an unknown feature, or
    # carrying an unregistered field is off-contract.
    "WhoAmI": [{"name": "router", "role": "router", "projects": [], "family": None},
               {"name": "router", "role": "router", "projects": [], "family": None,
                "features": "apply_ops.dry_run"},
               {"name": "router", "role": "router", "projects": [], "family": None,
                "features": ["apply_ops.dry_run", "nope"]},
               {"name": "router", "role": "router", "projects": [], "family": None,
                "features": [], "extra": 1}],
    "Claim": [{"key": "demo:A", "epoch": "1", "lease_until": "t"}, {"key": "k"}, {"key": "k", "epoch": True,
                                                                            "lease_until": "t"}],
    "Blocked": [{"demo:A": "demo:B"}, [1]],
    "Waves": [["a", 1], {"a": []}],
    "Percent": ["1", True, None],
}


@pytest.mark.parametrize("use_library", [False, True])
def test_validator_accepts_and_rejects(use_library):
    for name, value in GOOD.items():
        assert cs.problems(value, name, use_library) == [], name
    for name, values in BAD.items():
        for value in values:
            assert cs.problems(value, name, use_library), (name, value)


def test_validator_handles_events_and_oneof():
    event = {"seq": 1, "ts": "2030-01-01T00:00:00Z", "actor": None, "role": None, "model": None,
             "key": None, "type": "x", "payload": {}, "prev_hash": "", "hash": "a" * 64}
    assert cs.problems(event, "Event", False) == []
    assert cs.problems({**event, "hash": "zz"}, "Event", False)
    assert cs.problems({**event, "extra": 1}, "Event", False)
    assert cs.problems({"items": []}, "CreateItems", False) == []
    assert cs.problems([], "CreateItems", False) == []
    assert cs.problems({"nope": 1}, "CreateItems", False)
    json.dumps(cs.CONTRACT)
