"""PG-ANSWER-UI: the keeper HTTP hop for the operator principal.

The operator bearer is held only by the nginx sidecar; every request must also
carry the Authelia user in ``X-PG-Operator-User``, which must be allow-listed,
plus (for the write) an exact ``Origin`` and an hour-bound CSRF token.  A
non-operator client that sends the user header is refused, so a header any
direct client can forge never grants anything.

Every case here runs against a real ``ThreadingHTTPServer`` on an ephemeral
127.0.0.1 port, with tokens minted by ``auth.add_principal`` at test time and
the CSRF key in a tmp file — no credential literal appears in this file.
"""
import http.client
import json
import os
import secrets
import threading

import pytest

from keeper_util import World, defn, to_claimed
from plangraph.keeper import api, api_operator, auth, config
from plangraph.keeper.events import read_events, verify_integrity

ORIGIN = "https://plan.example.test"
USER = "ops-admin"
PEER = "ops-peer"
CHOICES = {"options": [{"id": "ship", "label": "Ship it", "consequence": "go now"},
                       {"id": "hold", "label": "Hold off"}],
           "default": "hold", "deadline": None, "blocks": None}

HOP_ROLES = ["router", "l1main", "workerA", "reviewer", "reader", "spawner"]


class FakeRuns:
    """Just enough of the run record to mint a writer token; never touches disk."""

    def valid_run_id(self, run_id):
        return run_id == "run-1"

    def job(self, run_id):
        if run_id != "run-1":
            raise FileNotFoundError(run_id)
        return {"request": {"title": "work on demo:L"}, "task": ""}


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    world.store.create_items(world.p("router"), [
        defn("Q", kind="question", operator=True, wait="answer", choices=CHOICES),
        defn("PLAIN", kind="question", operator=False, wait="answer",
             choices=CHOICES),
        defn("L"),
        defn("G", ["Q", "PLAIN", "L"], kind="goal")])
    world.store.ctx = {"runs": FakeRuns()}
    for name, role in (("operator", "operator"), ("spawner", "spawner")):
        token = auth.add_principal(world.conn, name, role, ["demo"])
        world.tokens[name] = token
        world.principals[name] = auth.authenticate(world.conn, token)
    yield world
    world.close()


@pytest.fixture
def key_file(tmp_path):
    """The CSRF MAC key file, 32 random bytes at test time (never a literal)."""
    path = tmp_path / "operator_csrf.key"
    path.write_text(secrets.token_hex(32), encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def make_cfg(tmp_path, world, *, users=(USER, PEER), origin=ORIGIN,
             key_file=None):
    data = {"db": str(world.path),
            "snapshot": str(tmp_path / "plan.snapshot.json"),
            "bind": "127.0.0.1:0",
            "operator_users": list(users)}
    if origin is not None:
        data["operator_origin"] = origin
    if key_file is not None:
        data["operator_csrf_key_file"] = str(key_file)
    path = tmp_path / "keeper.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    os.chmod(path, 0o600)
    return config.load_config(path)


@pytest.fixture
def serve(w, tmp_path):
    """Start servers on port 0 over the fixture store; all are stopped after."""
    live = []

    def _serve(**kw):
        server = api.make_server(w.store, make_cfg(tmp_path, w, **kw), port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        live.append((server, thread))
        return server

    yield _serve
    for server, thread in live:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def srv(serve, key_file):
    return serve(key_file=key_file)


def call(server, path, *, method="GET", body=None, token=None, headers=()):
    """One real request; ``headers`` is a list so a header can repeat."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    conn = http.client.HTTPConnection(*server.server_address, timeout=5)
    conn.putrequest(method, path)
    if token:
        conn.putheader("Authorization", f"Bearer {token}")
    if data is not None:
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(len(data)))
    for name, value in headers:
        conn.putheader(name, value)
    conn.endheaders(data)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    return resp.status, raw


def j(raw):
    return json.loads(raw.decode())


def hop(user=USER, origin=ORIGIN, csrf=None):
    headers = []
    if user is not None:
        headers.append((api_operator.HDR_USER, user))
    if origin is not None:
        headers.append(("Origin", origin))
    if csrf is not None:
        headers.append((api_operator.HDR_CSRF, csrf))
    return headers


def csrf_for(server, token, user=USER):
    """The token for *user*, minted by the API itself."""
    status, raw = call(server, "/v1/operator_csrf", token=token,
                       headers=hop(user=user))
    assert status == 200, raw
    return j(raw)["csrf"]


def minted(w, key_file, user=USER):
    """A CSRF token minted off the store clock from the key file."""
    key = key_file.read_text(encoding="utf-8").strip().encode("utf-8")
    return api_operator.csrf_token(key, user, w.clock())


def answer(server, w, key_file, *, key="demo:Q", token=None, body=None,
           user=USER, origin=ORIGIN, csrf=...):
    if body is None:
        body = {"key": key, "choice_id": "ship"}
    if csrf is ...:
        csrf = minted(w, key_file)
    return call(server, "/v1/operator_answer", method="POST", body=body,
                token=token or w.tok("operator"),
                headers=hop(user=user, origin=origin, csrf=csrf))


def audit_rows(w, since):
    """Audit rows appended since ``since`` (a ``COUNT(*)`` snapshot)."""
    return w.conn.execute(
        "SELECT ts,actor,role,action,key,ok,code,msg FROM audit_log "
        "WHERE rowid > ?", (since,)).fetchall()


def audit_count(w):
    return w.conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]


def event_count(w):
    return len(read_events(w.conn))


# --------------------------------------------------------------------------- #
# the happy path
# --------------------------------------------------------------------------- #

def test_operator_answers_by_choice_id(srv, w, key_file):
    token = w.tok("operator")
    csrf = csrf_for(srv, token)
    status, raw = answer(srv, w, key_file, token=token, csrf=csrf)
    assert status == 200, raw
    assert j(raw) == {"key": "demo:Q", "state": "done", "choice_id": "ship",
                      "source": "ui"}
    assert w.store._item("demo:Q")["state"] == "done"
    event = [ev for ev in read_events(w.conn) if ev["type"] == "answered"][-1]
    assert event["payload"]["source"] == "ui"
    assert event["payload"]["user"] == USER
    assert verify_integrity(w.conn) == []


def test_csrf_endpoint_is_scoped_to_the_header_user(srv, w):
    token = w.tok("operator")
    mine, theirs = csrf_for(srv, token), csrf_for(srv, token, user=PEER)
    assert mine != theirs and len(mine) == 64


# --------------------------------------------------------------------------- #
# the CSRF token
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("csrf", [None, "0" * 64])
def test_a_missing_or_wrong_csrf_is_refused(srv, w, key_file, csrf):
    status, raw = answer(srv, w, key_file, csrf=csrf)
    assert (status, j(raw)["error"]) == (403, "forbidden")
    assert w.store._item("demo:Q")["state"] == "open"


def test_a_csrf_minted_for_another_user_is_refused(srv, w, key_file):
    status, raw = answer(srv, w, key_file, csrf=minted(w, key_file, user=PEER))
    assert (status, j(raw)["error"]) == (403, "forbidden")
    assert w.store._item("demo:Q")["state"] == "open"


def test_a_csrf_two_windows_old_is_refused(srv, w, key_file):
    stale = csrf_for(srv, w.tok("operator"))
    w.clock.advance(2 * 3600)          # one window back still passes, two do not
    status, raw = answer(srv, w, key_file, csrf=stale)
    assert (status, j(raw)["error"]) == (403, "forbidden")
    w.clock.advance(-3600)
    assert answer(srv, w, key_file, csrf=stale)[0] == 200


def test_no_key_file_configured_is_refused(serve, w, key_file):
    server = serve()          # operator_csrf_key_file absent: nothing to check
    status, raw = answer(server, w, key_file)
    assert (status, j(raw)["error"]) == (403, "forbidden")


def test_a_short_key_file_fails_closed(serve, w, tmp_path):
    tiny = tmp_path / "tiny.key"
    tiny.write_text("tooshort", encoding="utf-8")
    server = serve(key_file=tiny)
    status, _ = call(server, "/v1/operator_csrf", token=w.tok("operator"),
                     headers=hop())
    assert status == 403


# --------------------------------------------------------------------------- #
# the user header
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("user", [None, "", "someone-else", "Ops-Admin",
                                  PEER + "-x"])
def test_a_user_header_that_is_not_allow_listed_is_refused(srv, w, key_file,
                                                            user):
    status, raw = answer(srv, w, key_file, user=user)
    assert (status, j(raw)["error"]) == (403, "forbidden")
    assert w.store._item("demo:Q")["state"] == "open"


def test_an_empty_allow_list_refuses_everyone(serve, w, key_file):
    server = serve(users=(), key_file=key_file)
    status, _ = call(server, "/v1/operator_csrf", token=w.tok("operator"),
                     headers=hop())
    assert status == 403
    assert answer(server, w, key_file, csrf=minted(w, key_file))[0] == 403


def test_a_name_outside_the_allow_list_with_its_own_csrf_is_refused(srv, w,
                                                                    key_file):
    """The allow-list is the grant: a self-minted token proves nothing."""
    stranger = "ops-not-listed"
    status, raw = answer(srv, w, key_file, user=stranger,
                         csrf=minted(w, key_file, user=stranger))
    assert (status, j(raw)["error"]) == (403, "forbidden")
    assert w.store._item("demo:Q")["state"] == "open"


def test_two_user_headers_are_refused(srv, w, key_file):
    headers = hop(csrf=minted(w, key_file))
    headers = [(api_operator.HDR_USER, USER)] + headers
    status, _ = call(srv, "/v1/operator_answer", method="POST",
                     body={"key": "demo:Q", "choice_id": "ship"},
                     token=w.tok("operator"), headers=headers)
    assert status == 403


# --------------------------------------------------------------------------- #
# the Origin
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("origin", [None, "https://other.example.test",
                                    ORIGIN + "/ui", "null"])
def test_an_origin_that_is_not_the_configured_one_is_refused(srv, w, key_file,
                                                             origin):
    status, raw = answer(srv, w, key_file, origin=origin)
    assert (status, j(raw)["error"]) == (403, "forbidden")
    assert w.store._item("demo:Q")["state"] == "open"


def test_operator_origin_unset_refuses_the_write(serve, w, key_file):
    server = serve(origin=None, key_file=key_file)
    status, _ = answer(server, w, key_file, csrf=minted(w, key_file),
                       origin=None)
    assert status == 403
    # the read door still needs no Origin
    assert call(server, "/v1/operator_csrf", token=w.tok("operator"),
                headers=hop(user=USER, origin=None))[0] == 200


# --------------------------------------------------------------------------- #
# the direct hop: any other bearer, with or without the header
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("who", HOP_ROLES)
def test_a_non_operator_sending_the_user_header_is_refused(srv, w, key_file,
                                                           who):
    csrf = minted(w, key_file)
    for method, path, body in (("GET", "/v1/operator_csrf", None),
                               ("POST", "/v1/operator_answer",
                                {"key": "demo:Q", "choice_id": "ship"})):
        status, raw = call(srv, path, method=method, body=body,
                           token=w.tok(who),
                           headers=hop(csrf=csrf))
        assert (status, j(raw)["error"]) == (403, "forbidden"), (who, path)
    assert w.store._item("demo:Q")["state"] == "open"


def test_a_run_token_sending_the_user_header_is_refused(srv, w, key_file):
    to_claimed(w, "demo:L")
    run = w.store.mint_run_token(w.p("l1main"), "demo:L", "writer", "run-1")
    status, raw = answer(srv, w, key_file, token=run["token"])
    assert (status, j(raw)["error"]) == (403, "forbidden")
    assert w.store._item("demo:Q")["state"] == "open"


@pytest.mark.parametrize("who", HOP_ROLES)
def test_a_non_operator_reaching_an_operator_verb_is_refused(srv, w, who):
    for method, path in (("GET", "/v1/operator_csrf"),
                         ("POST", "/v1/operator_answer")):
        status, raw = call(srv, path, method=method,
                           body={"key": "demo:Q", "choice_id": "ship"},
                           token=w.tok(who))
        assert (status, j(raw)["error"]) == (403, "forbidden"), (who, path)


# --------------------------------------------------------------------------- #
# what the operator may and may not call
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("method,path,body", [
    ("POST", "/v1/create_items", {"items": []}),
    ("POST", "/v1/answer", {"key": "demo:Q", "text": "x"}),
    ("POST", "/v1/claim", {"key": "demo:Q"}),
    ("POST", "/v1/close_on_decision", {"key": "demo:Q", "batch_ref": "b"}),
    ("GET", "/v1/ready", None),
    ("GET", "/v1/show?key=demo:Q", None),
])
def test_the_operator_is_limited_to_its_three_doors(srv, w, method, path, body):
    status, raw = call(srv, path, method=method, body=body,
                       token=w.tok("operator"), headers=hop())
    assert (status, j(raw)["error"]) == (403, "forbidden")
    assert w.store._item("demo:Q")["state"] == "open"


def test_whoami_is_allowed_with_the_user_header(srv, w):
    status, raw = call(srv, "/v1/whoami", token=w.tok("operator"), headers=hop())
    assert status == 200
    assert j(raw)["role"] == "operator"


def test_whoami_without_the_user_header_is_refused(srv, w):
    assert call(srv, "/v1/whoami", token=w.tok("operator"))[0] == 403


# --------------------------------------------------------------------------- #
# body validation, still behind the gate
# --------------------------------------------------------------------------- #

def test_wrong_choice_id_is_a_validation_error(srv, w, key_file):
    status, raw = answer(srv, w, key_file,
                         body={"key": "demo:Q", "choice_id": "nope"})
    assert (status, j(raw)["error"]) == (400, "validation")
    assert w.store._item("demo:Q")["state"] == "open"


@pytest.mark.parametrize("body", [
    {"key": "demo:Q", "choice_id": "ship", "actor": "router"},
    {"key": "demo:Q", "choice_id": "ship", "user": "somebody"},
    {"key": "demo:Q", "choice_id": "ship", "decision_id": "D-1"},
])
def test_an_extra_body_field_is_a_validation_error(srv, w, key_file, body):
    status, raw = answer(srv, w, key_file, body=body)
    assert (status, j(raw)["error"]) == (400, "validation")
    assert w.store._item("demo:Q")["state"] == "open"


def test_a_non_operator_item_is_refused(srv, w, key_file):
    status, raw = answer(srv, w, key_file, key="demo:PLAIN")
    assert 400 <= status < 500
    assert w.store._item("demo:PLAIN")["state"] == "open"


# --------------------------------------------------------------------------- #
# audit and leakage
# --------------------------------------------------------------------------- #

def refusals(srv, w, key_file):
    """Every gate-shaped refusal, as ``(label, callable)``."""
    csrf = minted(w, key_file)
    return [
        ("no user header", lambda: call(
            srv, "/v1/operator_answer", method="POST",
            body={"key": "demo:Q", "choice_id": "ship"},
            token=w.tok("operator"), headers=hop(user=None, csrf=csrf))),
        ("unknown user", lambda: answer(srv, w, key_file, user="mallory")),
        ("two users", lambda: call(
            srv, "/v1/operator_csrf", token=w.tok("operator"),
            headers=[(api_operator.HDR_USER, USER)] * 2)),
        ("no csrf", lambda: answer(srv, w, key_file, csrf=None)),
        ("bad csrf", lambda: answer(srv, w, key_file, csrf="f" * 64)),
        ("no origin", lambda: answer(srv, w, key_file, origin=None)),
        ("wrong verb", lambda: call(srv, "/v1/ready", token=w.tok("operator"),
                                    headers=hop())),
        ("direct hop", lambda: call(srv, "/v1/operator_csrf",
                                    token=w.tok("router"), headers=hop())),
    ]


def test_every_refusal_audits_exactly_one_row_and_no_event(srv, w, key_file):
    for label, fire in refusals(srv, w, key_file):
        before, events = audit_count(w), event_count(w)
        status, raw = fire()
        assert status == 403, (label, status, raw)
        rows = audit_rows(w, before)
        assert len(rows) == 1, (label, [dict(r) for r in rows])
        assert dict(rows[0])["action"] == "authenticate"
        assert event_count(w) == events, label
    assert w.store._item("demo:Q")["state"] == "open"


def test_a_refusal_never_echoes_a_credential(srv, w, key_file):
    token = w.tok("operator")
    secret = key_file.read_text(encoding="utf-8").strip()
    csrf = minted(w, key_file)
    for label, fire in refusals(srv, w, key_file):
        before = audit_count(w)
        _, raw = fire()
        for forbidden in (token, secret, csrf):
            assert forbidden not in raw.decode(), label
            for row in audit_rows(w, before):
                assert forbidden not in str(dict(row)["msg"]), label


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #

def cfg(tmp_path, **extra):
    path = tmp_path / "keeper.json"
    data = {"db": "k.db", "bind": "127.0.0.1:0"}
    data.update(extra)
    path.write_text(json.dumps(data), encoding="utf-8")
    os.chmod(path, 0o600)
    return config.load_config(path)


def test_operator_keys_default_to_closed(tmp_path):
    c = cfg(tmp_path)
    assert c.operator_users == []
    assert c.operator_origin is None
    assert c.operator_csrf_key_file is None


def test_operator_keys_load(tmp_path):
    c = cfg(tmp_path, operator_users=[USER], operator_origin=ORIGIN,
            operator_csrf_key_file="operator_csrf.key")
    assert c.operator_users == [USER]
    assert c.operator_origin == ORIGIN
    assert c.operator_csrf_key_file == str(tmp_path / "operator_csrf.key")


@pytest.mark.parametrize("users", ["ops", 7, [7], ["ops-admin", ""]])
def test_bad_operator_users_are_refused(tmp_path, users):
    with pytest.raises(config.ConfigError):
        cfg(tmp_path, operator_users=users)


@pytest.mark.parametrize("origin", ["plan.example.test", "https://",
                                    "https://plan.example.test/",
                                    "https://plan.example.test/ui",
                                    "https://plan.example.test?x=1",
                                    "ftp://plan.example.test",
                                    "https://user@plan.example.test", 7])
def test_bad_operator_origin_is_refused(tmp_path, origin):
    with pytest.raises(config.ConfigError):
        cfg(tmp_path, operator_origin=origin)


@pytest.mark.parametrize("origin", [ORIGIN, "https://plan.example.test:8443",
                                    "http://127.0.0.1:8765"])
def test_good_operator_origin_loads(tmp_path, origin):
    assert cfg(tmp_path, operator_origin=origin).operator_origin == origin


def test_repr_names_keys_never_values(tmp_path):
    c = cfg(tmp_path, operator_users=[USER], operator_origin=ORIGIN,
            operator_csrf_key_file="operator_csrf.key")
    text = repr(c)
    assert "operator_users" in text and "operator_csrf_key_file" in text
    for value in (USER, ORIGIN, "ops"):
        assert value not in text
