"""tools/apply_batch.py with a granted role-l1 token (KEEPER-PROJECT-SCOPED-L1-APPLY)."""
import json
import urllib.error
import urllib.request

import yaml

from test_apply_batch import (  # noqa: F401  (fixtures)
    item_count, ops_for, run, srv, state_of, world, write_batch)


def l1_batch(tmp_path, ops=None, name="l1batch"):
    path = write_batch(tmp_path, ops=ops, name=name)
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["principal"] = "l1main"
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return path


def token(world, tmp_path, who):
    path = tmp_path / f"{who}.token"
    path.write_text(world.tokens[who], encoding="utf-8")
    return str(path)


def call(srv, verb, tok, body, method="POST"):
    request = urllib.request.Request(
        f"{srv.base_url}/v1/{verb}", method=method,
        data=json.dumps(body).encode() if method == "POST" else None,
        headers={"Authorization": f"Bearer {tok}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as reply:
            return reply.status, json.loads(reply.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def test_granted_l1_token_applies_a_batch_and_reports_to_routing(world, srv, tmp_path):
    world.store.grant_apply(world.p("router"), "l1main", "demo")
    rc, out, _ = run(l1_batch(tmp_path), srv.base_url, token(world, tmp_path, "l1main"))
    assert rc == 0, out
    assert "acting as l1main (l1)" in out
    assert state_of(world, "demo:B") == "done"
    rows = world.conn.execute(
        "SELECT body, \"from\" FROM messages WHERE \"to\"='routing'").fetchall()
    assert len(rows) == 1 and rows[0]["from"] == "l1main"
    body = json.loads(rows[0]["body"])
    assert body["batch"] == "X7d-l1batch" and body["ops"] == 6
    assert body["source"].endswith("l1batch.md") and len(body["sha256"]) == 64


def test_foreign_key_in_a_granted_l1_batch_refuses_all(world, srv, tmp_path):
    world.store.grant_apply(world.p("router"), "l1main", "demo")
    ops = ops_for() + [{"op": "cancel", "key": "other:Z", "reason": "r"}]
    rc, out, _ = run(l1_batch(tmp_path, ops=ops), srv.base_url,
                     token(world, tmp_path, "l1main"))
    assert rc == 1 and "other:Z" in out
    assert item_count(world) == 0


def test_ungranted_l1_token_is_refused_before_any_call(world, srv, tmp_path):
    rc, out, _ = run(l1_batch(tmp_path), srv.base_url, token(world, tmp_path, "l1main"))
    assert rc == 1 and "principal mismatch" in out
    assert item_count(world) == 0


def test_grant_verbs_over_http_are_router_only(world, srv, tmp_path):
    body = {"principal": "l1main", "project": "demo"}
    status, _ = call(srv, "grant_apply", world.tokens["l1main"], body)
    assert status == 403
    status, reply = call(srv, "grant_apply", world.tokens["router"], body)
    assert status == 200, reply
    status, reply = call(srv, "apply_grants", world.tokens["l1main"], None, "GET")
    assert status == 200
    assert [g["project"] for g in reply["grants"]] == ["demo"]
    status, _ = call(srv, "revoke_apply", world.tokens["l1main"], body)
    assert status == 403
    status, _ = call(srv, "revoke_apply", world.tokens["router"], body)
    assert status == 200


def test_cli_grant_apply_round_trip(world, srv, tmp_path, capsys, monkeypatch):
    from plangraph.keeper import cli
    cfg = tmp_path / "client.json"
    for who, argv in (("router", ["grant-apply", "l1main", "demo"]),
                      ("l1main", ["grant-apply", "l1other", "other"]),
                      ("router", ["grant-apply", "--list"]),
                      ("router", ["grant-apply", "l1main", "demo", "--revoke"])):
        tok = tmp_path / f"{who}.tok"
        tok.write_text(world.tokens[who], encoding="utf-8")
        tok.chmod(0o600)
        monkeypatch.setenv("PG_URL", srv.base_url)
        monkeypatch.setenv("PG_TOKEN_FILE", str(tok))
        rc = cli.main(argv)
        out = capsys.readouterr()
        if who == "router":
            assert rc == 0, out
        else:
            assert rc != 0
    assert world.store._grant_projects("l1main") == set()
