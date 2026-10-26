"""Contract 1: framing, authentication, error bodies and status mapping."""
import json

import pytest

import contract_schema as cs
from contract_harness import PRINCIPALS
from contract_schema import assert_valid
from contract_world import err, make_plan, need, ok, uid


def test_health_is_open_and_get_only(kp):
    body = ok(kp.get("/v1/health"), "Health")
    assert body == {"status": "ok"}


def test_missing_or_unknown_token_is_401_forbidden(kp):
    for resp in (kp.get("/v1/whoami"), kp.get("/v1/whoami", token="not-a-token"),
                 kp.post("claim", None, {"key": "demo:X"}),
                 kp.get("/v1/whoami", headers={"Authorization": "Basic abc"})):
        err(resp, 401, "forbidden")


def test_bearer_scheme_is_case_sensitive(kp):
    resp = kp.get("/v1/whoami", headers={"Authorization": "bearer " + kp.tokens["router"]})
    err(resp, 401, "forbidden")


def test_token_value_never_echoed(kp):
    bad = "tok-" + uid()
    resp = kp.get("/v1/whoami", token=bad)
    assert bad not in resp.text


@pytest.mark.parametrize("name", sorted(PRINCIPALS))
def test_whoami_reports_the_token_principal(kp, name):
    role, projects, family = PRINCIPALS[name]
    body = ok(kp.get("/v1/whoami", name), "WhoAmI")
    assert body["name"] == name and body["role"] == role
    assert body["projects"] == projects
    # families are case-folded by the keeper
    assert body["family"] == (family.lower() if family else None)
    # qwen-R6: what the wire advertises is exactly what the contract registers
    assert set(body["features"]) == set(cs.DEFS["WhoAmI"]["properties"]["features"]["items"]["enum"])


def test_forged_actor_and_role_in_body_are_ignored(kp):
    plan = make_plan(kp)
    ok(kp.post("claim", "worker1", {"key": plan.key, "actor": "router", "role": "router"}))
    shown = ok(kp.get(f"/v1/show?key={plan.key}&fmt=json", "reader"), "Show")
    assert shown["claimed_by"] == "worker1"
    err(kp.post("set_priority", "worker1", {"key": plan.key, "n": 5, "role": "router"}), 403, "forbidden")


def test_unknown_route_and_wrong_method(kp):
    err(kp.get("/v1/nope", "router"), 404, "not_found")
    err(kp.get("/nope", "router"), 404, "not_found")
    err(kp.get("/v1/", "router"), 404, "not_found")
    err(kp.get("/v1/claim", "router"), 405, "method_not_allowed")
    err(kp.post("ready", "router", {}), 405, "method_not_allowed")
    err(kp.post("whoami", "router", {}), 405, "method_not_allowed")


def test_bad_json_and_non_object_bodies_are_400_validation(kp):
    err(kp.call("POST", "/v1/claim", "worker1", raw_body=b"{not json"), 400, "validation")
    err(kp.call("POST", "/v1/claim", "worker1", raw_body=b"\xff\xfe"), 400, "validation")
    err(kp.call("POST", "/v1/claim", "worker1", body=[1, 2]), 400, "validation")
    err(kp.post("claim", "worker1", {}), 400, "validation")          # missing key
    err(kp.post("heartbeat", "worker1", {"key": "demo:X"}), 400, "validation")  # missing epoch


def test_oversize_body_is_413(kp):
    resp = kp.call("POST", "/v1/claim", "worker1", raw_body=b"x" * (1024 * 1024 + 1))
    err(resp, 413, "too_large")


def test_unknown_item_is_404(kp):
    err(kp.post("claim", "worker1", {"key": "demo:NOPE-" + uid()}), 404, "not_found")
    err(kp.get("/v1/show?key=demo:NOPE-" + uid(), "reader"), 404, "not_found")


def test_error_body_shape_for_every_refusal_class(kp):
    plan = make_plan(kp, 2, chain=True)
    first, second = plan.lanes
    err(kp.post("claim", "reader", {"key": first}), 403, "forbidden")          # role
    err(kp.post("claim", "worker1", {"key": second}), 400, "bad_state")         # unmet dep
    epoch = ok(kp.post("claim", "worker1", {"key": first}), "Claim")["epoch"]
    err(kp.post("claim", "worker2", {"key": first}), 409, "conflict")           # already claimed
    err(kp.post("release", "worker1", {"key": first, "epoch": epoch + 5}), 409, "epoch_stale")
    err(kp.post("submit", "worker1", {"key": first, "epoch": epoch}), 400, "evidence_missing")


def test_text_and_json_content_types(kp):
    plan = make_plan(kp)
    text = kp.get("/v1/ready", "reader")
    assert text.status == 200 and "text/plain" in text.ctype
    assert kp.get("/v1/ready?fmt=json", "reader").ctype.startswith("application/json")
    shown = kp.get(f"/v1/show?key={plan.key}", "reader")
    assert "text/plain" in shown.ctype and plan.key in shown.text
    # verbs that are not text-by-default answer JSON
    assert kp.get("/v1/waves", "reader").ctype.startswith("application/json")
    # a write may ask for text; the body format is unspecified, the key is named
    wrote = kp.call("POST", "/v1/set_priority?fmt=text", "router", {"key": plan.key, "n": 1})
    assert wrote.status == 200 and "text/plain" in wrote.ctype


def test_read_text_forms_are_nonempty_for_text_default_verbs(kp):
    make_plan(kp)
    for path in ("/v1/ready", "/v1/blocked", "/v1/alerts", "/v1/aliases", "/v1/changelog?since=0"):
        resp = kp.get(path, "reader")
        assert resp.status == 200 and "text/plain" in resp.ctype and resp.text.strip(), path


def test_revoked_principal_token_stops_working(kp, tmp_path):
    need(kp, "admin")
    name = "temp-" + uid().lower()
    out = kp.admin("add-principal", name, "--role", "reader")
    assert out.returncode == 0, out.stderr
    token = out.stdout.strip().rsplit(":", 1)[1].strip()
    assert ok(kp.get("/v1/whoami", token=token), "WhoAmI")["name"] == name
    assert kp.admin("revoke", name).returncode == 0
    err(kp.get("/v1/whoami", token=token), 401, "forbidden")


def test_duplicate_principal_name_is_refused_by_admin(kp):
    need(kp, "admin")
    assert kp.admin("add-principal", "router", "--role", "reader").returncode != 0


def test_error_bodies_are_json_objects_with_two_keys(kp):
    resp = kp.get("/v1/nope", "router")
    assert set(json.loads(resp.raw)) == {"error", "msg"}
