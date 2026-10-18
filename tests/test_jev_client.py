"""Hermetic tests for the Jev shadow client (no external network).

Everything talks to a fake ``ThreadingHTTPServer`` on 127.0.0.1, to the
deterministic stub, or to a fake ``opener``; no real Jev endpoint is ever
contacted and no real key exists.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from plangraph.planner import jev_client as jev

SECRET = "test-key-not-real"


class _FakeServer:
    """Configurable fake HTTP endpoint that records what it receives."""

    def __init__(self):
        self.status = 200
        self.body = b"{}"
        self.delay = 0.0
        self.requests: list = []
        self.raw_bodies: list = []
        self.headers_seen: list = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep test output quiet
                pass

            def _handle(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                outer.raw_bodies.append(raw)
                outer.headers_seen.append(
                    {name.lower(): value for name, value in self.headers.items()})
                try:
                    outer.requests.append(json.loads(raw.decode("utf-8")))
                except Exception:
                    outer.requests.append(None)
                if outer.delay:
                    time.sleep(outer.delay)
                payload = outer.body
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                try:
                    if payload:
                        self.wfile.write(payload)
                except Exception:
                    pass  # client may have timed out and gone away

            do_POST = _handle
            do_GET = _handle

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return "http://%s:%d/" % (host, port)

    def start(self) -> "_FakeServer":
        thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def fake_server():
    server = _FakeServer().start()
    try:
        yield server
    finally:
        server.stop()


def _question(instructions="pick one"):
    return {"type": "choice", "instructions": instructions,
            "criteria": {"a": "first", "b": "second", "c": "third"}}


def _answer_json(**overrides):
    base = {"type": "choice", "choice": "a",
            "probabilities": {"a": 1.0, "b": 0.0, "c": 0.0}, "confidence": 0.5}
    base.update(overrides)
    return base


def _ok_body(model="resp-model", answers=None):
    return json.dumps({"model": model, "answers": answers or {},
                       "usage": {"input_tokens": 3, "output_tokens": 4}}).encode("utf-8")


def _cfg(**overrides):
    base = dict(mode="api", url="http://127.0.0.1:1/", model="test-model",
                key_source="env:JEV_API_KEY", timeout_s=8.0)
    base.update(overrides)
    return jev.JevConfig(**base)


def _no_key_env(tmp_path):
    """Env for auth_store that can never find anything (no real store touched)."""
    return {"PLANGRAPH_JEV_AUTH_PATH": str(tmp_path / "absent-auth.json"),
            "PLANGRAPH_JEV_AUTH_DB": str(tmp_path / "absent.db")}


def _make_credential_db(path, rows):
    """Create the auth table the client reads, with id, integration, active and key columns."""
    connection = sqlite3.connect(str(path))
    try:
        connection.execute("CREATE TABLE credential "
                           "(id TEXT, integration TEXT, active INTEGER, key TEXT)")
        connection.executemany(
            "INSERT INTO credential (id, integration, active, key) VALUES (?, ?, ?, ?)",
            rows)
        connection.commit()
    finally:
        connection.close()


def _boom(*args, **kwargs):
    raise AssertionError("opener must not be called")


# --------------------------------------------------------------------------- #
# 1. config_from_env
# --------------------------------------------------------------------------- #

def test_config_defaults_are_off_and_zen():
    cfg = jev.config_from_env({})
    assert cfg.mode == "off"
    assert cfg.url == jev.DEFAULT_URL
    assert cfg.model == jev.DEFAULT_MODEL
    assert cfg.key_source == "auth_store"
    assert cfg.timeout_s == jev.DEFAULT_TIMEOUT_S
    assert jev.enabled(cfg) is False


def test_config_profiles_and_overrides():
    zen = jev.config_from_env({"PLANGRAPH_JEV_PROFILE": "zen"})
    assert (zen.url, zen.model, zen.key_source) == (
        jev.DEFAULT_URL, jev.DEFAULT_MODEL, "auth_store")
    typesafe = jev.config_from_env({"PLANGRAPH_JEV_PROFILE": "typesafe"})
    assert (typesafe.url, typesafe.model, typesafe.key_source) == (
        "https://api.typesafe.ai/v1/systemone", "jev-latest", "env:JEV_API_KEY")
    unknown = jev.config_from_env({"PLANGRAPH_JEV_PROFILE": "nope"})
    assert unknown.url == jev.DEFAULT_URL
    overridden = jev.config_from_env({
        "PLANGRAPH_JEV_PROFILE": "typesafe",
        "PLANGRAPH_JEV_URL": "http://127.0.0.1:9/x",
        "PLANGRAPH_JEV_MODEL": "m2",
        "PLANGRAPH_JEV_KEY_SOURCE": "env:OTHER_KEY"})
    assert (overridden.url, overridden.model, overridden.key_source) == (
        "http://127.0.0.1:9/x", "m2", "env:OTHER_KEY")


def test_config_mode_parsing():
    assert jev.config_from_env({"PLANGRAPH_JEV": "api"}).mode == "api"
    assert jev.config_from_env({"PLANGRAPH_JEV": "STUB"}).mode == "stub"
    assert jev.config_from_env({"PLANGRAPH_JEV": " off "}).mode == "off"
    assert jev.config_from_env({"PLANGRAPH_JEV": "on"}).mode == "auto"
    assert jev.config_from_env({"PLANGRAPH_JEV": "banana"}).mode == "off"
    assert jev.config_from_env({"PLANGRAPH_JEV": "AUTO"}).mode == "auto"


def test_config_timeout_parsing_and_clamp():
    assert jev.config_from_env({"PLANGRAPH_JEV_TIMEOUT_S": "1.5"}).timeout_s == 1.5
    assert (jev.config_from_env({"PLANGRAPH_JEV_TIMEOUT_S": "abc"}).timeout_s
            == jev.DEFAULT_TIMEOUT_S)
    assert (jev.config_from_env({"PLANGRAPH_JEV_TIMEOUT_S": "0"}).timeout_s
            == jev.DEFAULT_TIMEOUT_S)
    assert (jev.config_from_env({"PLANGRAPH_JEV_TIMEOUT_S": "-3"}).timeout_s
            == jev.DEFAULT_TIMEOUT_S)
    assert (jev.config_from_env({"PLANGRAPH_JEV_TIMEOUT_S": "nan"}).timeout_s
            == jev.DEFAULT_TIMEOUT_S)
    assert (jev.config_from_env({"PLANGRAPH_JEV_TIMEOUT_S": "999"}).timeout_s
            == jev.MAX_TIMEOUT_S)
    assert jev.config_from_env({"PLANGRAPH_JEV_TIMEOUT_S": "10"}).timeout_s == 10.0
    assert jev.config_from_env({"PLANGRAPH_JEV_TIMEOUT_S": "10.5"}).timeout_s == 10.0
    assert jev.config_from_env({"PLANGRAPH_JEV_TIMEOUT_S": "0.5"}).timeout_s == 0.5
    assert jev.DEFAULT_TIMEOUT_S <= jev.MAX_TIMEOUT_S


# --------------------------------------------------------------------------- #
# 2. stub_answer / stub mode
# --------------------------------------------------------------------------- #

def test_stub_is_deterministic_and_matches_hash():
    questions = {"q1": _question(), "q2": _question()}
    first = jev.stub_answer("state one", questions)
    second = jev.stub_answer("state one", questions)
    assert first == second
    for key, question in questions.items():
        options = list(question["criteria"].keys())
        digest = hashlib.sha256(("%s\0%s" % (key, "state one")).encode("utf-8")).hexdigest()
        expected = options[int(digest, 16) % len(options)]
        answer = first[key]
        assert answer.status == "ok"
        assert answer.choice == expected
        assert answer.choice in options
        assert set(answer.probabilities) == set(options)
        assert abs(sum(answer.probabilities.values()) - 1.0) < 1e-9
        assert answer.probabilities[expected] == 0.6
        assert answer.confidence == 0.6
        assert answer.model == "stub"
        assert answer.reason == "stub"
        assert answer.latency_ms == 0


def test_stub_single_option_and_empty_criteria():
    single = jev.stub_answer("s", {"one": {"type": "choice", "instructions": "",
                                           "criteria": {"only": "the one"}}})
    answer = single["one"]
    assert answer.status == "ok"
    assert answer.choice == "only"
    assert answer.probabilities == {"only": 1.0}
    assert answer.confidence == 1.0
    empty = jev.stub_answer("s", {"blank": {"type": "choice", "instructions": "",
                                            "criteria": {}}})
    assert empty["blank"].status == "error"
    assert empty["blank"].reason == "no options"
    missing = jev.stub_answer("s", {"none": {}})
    assert missing["none"].status == "error"
    assert missing["none"].reason == "no options"


def test_stub_never_touches_the_network():
    cfg = _cfg(mode="stub", url="http://127.0.0.1:1/")
    answers = jev.ask("state text", {"q": _question()}, cfg, env={}, opener=_boom)
    assert answers["q"].status == "ok"
    assert answers["q"].model == "stub"
    # no injected opener, url points at a closed port: still no network call
    plain = jev.ask("state text", {"q": _question()}, cfg, env={})
    assert plain == answers


# --------------------------------------------------------------------------- #
# 3. mode off
# --------------------------------------------------------------------------- #

def test_mode_off_returns_empty_and_never_calls_opener():
    cfg = _cfg(mode="off")
    assert jev.ask("s", {"q": _question()}, cfg, env={}, opener=_boom) == {}
    assert jev.enabled(cfg) is False
    assert jev.ask("s", {"q": _question()},
                   _cfg(mode="nonsense"), env={}, opener=_boom) == {}


# --------------------------------------------------------------------------- #
# 4. api happy path and resolve_key
# --------------------------------------------------------------------------- #

def test_api_happy_path_env_key(fake_server, tmp_path):
    fake_server.body = _ok_body("resp-model", {
        "q": {"type": "choice", "choice": "b",
              "probabilities": {"a": 0.2, "b": 0.7, "c": 0.1}, "confidence": 0.7}})
    cfg = _cfg(url=fake_server.url)
    env = dict(_no_key_env(tmp_path), JEV_API_KEY=SECRET)
    answers = jev.ask("the state", {"q": _question()}, cfg, env=env)
    answer = answers["q"]
    assert answer.status == "ok"
    assert answer.choice == "b"
    assert answer.probabilities == {"a": 0.2, "b": 0.7, "c": 0.1}
    assert answer.confidence == 0.7
    assert answer.model == "resp-model"
    assert answer.latency_ms is not None

    body = fake_server.requests[0]
    assert body["model"] == "test-model"
    assert body["state"] == "the state"
    assert body["questions"]["q"]["type"] == "choice"
    assert body["questions"]["q"]["instructions"] == "pick one"
    assert body["questions"]["q"]["criteria"] == {"a": "first", "b": "second",
                                                  "c": "third"}
    assert fake_server.headers_seen[0]["authorization"] == "Bearer " + SECRET
    assert fake_server.headers_seen[0]["content-type"] == "application/json"


def test_api_truncates_state(fake_server, tmp_path):
    fake_server.body = _ok_body("m", {"q": _answer_json()})
    cfg = _cfg(url=fake_server.url)
    env = dict(_no_key_env(tmp_path), JEV_API_KEY=SECRET)
    jev.ask("x" * (jev.MAX_STATE_CHARS + 500), {"q": _question()}, cfg, env=env)
    assert len(fake_server.requests[0]["state"]) == jev.MAX_STATE_CHARS


def test_api_happy_path_auth_store(fake_server, tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"opencode": {"type": "api", "key": SECRET}}),
                    encoding="utf-8")
    fake_server.body = _ok_body("m", {"q": _answer_json(choice="a")})
    cfg = _cfg(url=fake_server.url, key_source="auth_store")
    env = dict(_no_key_env(tmp_path), PLANGRAPH_JEV_AUTH_PATH=str(auth))
    answers = jev.ask("s", {"q": _question()}, cfg, env=env)
    assert answers["q"].status == "ok"
    assert fake_server.headers_seen[0]["authorization"] == "Bearer " + SECRET


def test_resolve_key_env_source():
    cfg = _cfg(key_source="env:JEV_API_KEY")
    assert jev.resolve_key(cfg, {"JEV_API_KEY": "  abc  "}) == "abc"
    assert jev.resolve_key(cfg, {"JEV_API_KEY": "   "}) is None
    assert jev.resolve_key(cfg, {}) is None
    assert jev.resolve_key(_cfg(key_source="env:"), {"ANY": "x"}) is None
    assert jev.resolve_key(_cfg(key_source="other"), {"JEV_API_KEY": "x"}) is None


def test_resolve_key_auth_json_edges(tmp_path):
    cfg = _cfg(key_source="auth_store")
    env = _no_key_env(tmp_path)
    assert jev.resolve_key(cfg, env) is None  # nothing anywhere
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert jev.resolve_key(cfg, dict(env, PLANGRAPH_JEV_AUTH_PATH=str(bad))) is None
    wrong = tmp_path / "wrong.json"
    wrong.write_text(json.dumps(["a", "list"]), encoding="utf-8")
    assert jev.resolve_key(cfg, dict(env, PLANGRAPH_JEV_AUTH_PATH=str(wrong))) is None
    shape = tmp_path / "shape.json"
    shape.write_text(json.dumps({"opencode": {"type": "api"}}), encoding="utf-8")
    assert jev.resolve_key(cfg, dict(env, PLANGRAPH_JEV_AUTH_PATH=str(shape))) is None


def test_resolve_key_auth_json_shapes_and_reread(tmp_path):
    cfg = _cfg(key_source="auth_store")
    path = tmp_path / "auth.json"
    env = _no_key_env(tmp_path)
    env["PLANGRAPH_JEV_AUTH_PATH"] = str(path)

    path.write_text(json.dumps({"opencode": {"key": "k1"}}), encoding="utf-8")
    assert jev.resolve_key(cfg, env) == "k1"
    path.write_text(json.dumps({"opencode": {"access": "k2"}}), encoding="utf-8")
    assert jev.resolve_key(cfg, env) == "k2"
    path.write_text(json.dumps({"opencode": "k3"}), encoding="utf-8")
    assert jev.resolve_key(cfg, env) == "k3"
    path.write_text(json.dumps({"opencode-zen": {"token": "k4"}}), encoding="utf-8")
    assert jev.resolve_key(cfg, env) == "k4"
    path.write_text(json.dumps({"zen": {"key": "k5"}}), encoding="utf-8")
    assert jev.resolve_key(cfg, env) == "k5"
    # the file is re-read on every call: a change is visible immediately
    path.write_text(json.dumps({"opencode": {"key": "k6"}}), encoding="utf-8")
    assert jev.resolve_key(cfg, env) == "k6"


def test_parse_answer_ok_and_variants():
    question = _question()
    good = jev.parse_answer(question, _answer_json(choice="b",
                                                  probabilities={"a": 0.1, "b": 0.8,
                                                                 "c": 0.1},
                                                  confidence=0.8),
                            "model-x", 7, key="q")
    assert good.status == "ok"
    assert good.choice == "b"
    assert good.probabilities == {"a": 0.1, "b": 0.8, "c": 0.1}
    assert good.confidence == 0.8
    assert good.model == "model-x"
    assert good.latency_ms == 7

    cases = [
        (None, "missing"),
        ("text", "missing"),
        (_answer_json(type="text"), "wrong type"),
        (_answer_json(choice="zzz"), "unknown choice"),
        (_answer_json(choice=None), "unknown choice"),
        (_answer_json(probabilities={"a": 0.6, "b": 0.6, "c": 0.6}), "bad probabilities"),
        (_answer_json(probabilities={"a": 0.5, "b": 0.5, "zz": 0.0}), "bad probabilities"),
        (_answer_json(probabilities={"a": "x", "b": 0.0, "c": 0.0}), "bad probabilities"),
        (_answer_json(probabilities="nope"), "bad probabilities"),
        (_answer_json(confidence="high"), "bad confidence"),
        (_answer_json(confidence=True), "bad confidence"),
        (_answer_json(confidence=None), "bad confidence"),
    ]
    for raw, phrase in cases:
        answer = jev.parse_answer(question, raw, "m", 0, key="q")
        assert answer.status == "error", raw
        assert answer.reason == "bad answer for q: " + phrase, raw


# --------------------------------------------------------------------------- #
# 5. status mapping through the fake server
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("code,status", [(429, "unavailable"), (529, "unavailable"),
                                         (401, "unavailable"), (403, "unavailable"),
                                         (500, "unavailable"), (503, "unavailable"),
                                         (400, "error"), (422, "error")])
def test_status_mapping_http_codes(fake_server, tmp_path, code, status):
    fake_server.status = code
    fake_server.body = b'{"error": "nope"}'
    cfg = _cfg(url=fake_server.url)
    env = dict(_no_key_env(tmp_path), JEV_API_KEY=SECRET)
    answer = jev.ask("s", {"q": _question()}, cfg, env=env)["q"]
    assert answer.status == status
    assert ("http %d" % code) in answer.reason


def test_connection_refused_is_unavailable():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    cfg = _cfg(url="http://127.0.0.1:%d/" % port, timeout_s=0.5)
    answer = jev.ask("s", {"q": _question()}, cfg,
                     env={"JEV_API_KEY": SECRET})["q"]
    assert answer.status == "unavailable"
    assert answer.reason.startswith("connect failed:")


def test_timeout_is_recorded_and_bounded(fake_server, tmp_path):
    fake_server.delay = 1.0
    fake_server.body = _ok_body("m", {"q": _answer_json()})
    cfg = _cfg(url=fake_server.url, timeout_s=0.3)
    env = dict(_no_key_env(tmp_path), JEV_API_KEY=SECRET)
    started = time.monotonic()
    answer = jev.ask("s", {"q": _question()}, cfg, env=env)["q"]
    elapsed = time.monotonic() - started
    assert answer.status == "timeout"
    assert answer.reason == "timeout after 0.3s"
    assert elapsed < 2.0


def test_bad_response_bodies(fake_server, tmp_path):
    cfg = _cfg(url=fake_server.url)
    env = dict(_no_key_env(tmp_path), JEV_API_KEY=SECRET)
    questions = {"q": _question()}
    for body in (b"not json at all", b"[1, 2, 3]", b'"a string"',
                 json.dumps({"model": "m"}).encode("utf-8"),
                 json.dumps({"model": "m", "answers": "nope"}).encode("utf-8")):
        fake_server.body = body
        answer = jev.ask("s", questions, cfg, env=env)["q"]
        assert answer.status == "error"
        assert answer.reason == "bad response body"


def test_per_key_bad_answers_isolated(fake_server, tmp_path):
    cfg = _cfg(url=fake_server.url)
    env = dict(_no_key_env(tmp_path), JEV_API_KEY=SECRET)
    questions = {"good": _question(), "bad": _question()}
    fake_server.body = json.dumps({"model": "m", "answers": {
        "good": _answer_json(choice="c"),
        "bad": _answer_json(choice="zzz"),
    }}).encode("utf-8")
    answers = jev.ask("s", questions, cfg, env=env)
    assert answers["good"].status == "ok"
    assert answers["good"].model == "m"
    assert answers["bad"].status == "error"
    assert answers["bad"].reason == "bad answer for bad: unknown choice"
    assert answers["bad"].model == "m"


def test_model_falls_back_to_config(fake_server, tmp_path):
    fake_server.body = json.dumps({"answers": {"q": _answer_json()}}).encode("utf-8")
    cfg = _cfg(url=fake_server.url)
    env = dict(_no_key_env(tmp_path), JEV_API_KEY=SECRET)
    assert jev.ask("s", {"q": _question()}, cfg, env=env)["q"].model == "test-model"


# --------------------------------------------------------------------------- #
# 6. missing key
# --------------------------------------------------------------------------- #

def test_missing_key_api_is_unavailable_and_no_call(tmp_path):
    cfg = _cfg(mode="api", url="http://127.0.0.1:1/")
    answers = jev.ask("s", {"q": _question(), "r": _question()}, cfg,
                      env=_no_key_env(tmp_path), opener=_boom)
    assert set(answers) == {"q", "r"}
    for answer in answers.values():
        assert answer.status == "unavailable"
        assert answer.reason == "no key"


def test_missing_key_auth_store_variants(tmp_path):
    cfg = _cfg(mode="api", url="http://127.0.0.1:1/", key_source="auth_store")
    env = _no_key_env(tmp_path)
    answer = jev.ask("s", {"q": _question()}, cfg, env=env, opener=_boom)["q"]
    assert (answer.status, answer.reason) == ("unavailable", "no key")
    garbage = tmp_path / "auth.json"
    garbage.write_text("{not json", encoding="utf-8")
    env2 = dict(env, PLANGRAPH_JEV_AUTH_PATH=str(garbage))
    answer2 = jev.ask("s", {"q": _question()}, cfg, env=env2, opener=_boom)["q"]
    assert (answer2.status, answer2.reason) == ("unavailable", "no key")


# --------------------------------------------------------------------------- #
# 6b. auto mode
# --------------------------------------------------------------------------- #

def test_auto_with_key_goes_live(fake_server, tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"opencode": {"key": SECRET}}), encoding="utf-8")
    fake_server.body = _ok_body("live-model", {"q": _answer_json(choice="a")})
    cfg = _cfg(mode="auto", url=fake_server.url, key_source="auth_store")
    env = dict(_no_key_env(tmp_path), PLANGRAPH_JEV_AUTH_PATH=str(auth))
    answers = jev.ask("s", {"q": _question()}, cfg, env=env)
    assert answers["q"].status == "ok"
    assert answers["q"].model == "live-model"
    assert len(fake_server.requests) == 1


def test_auto_without_key_uses_stub_offline(tmp_path):
    cfg = _cfg(mode="auto", url="http://127.0.0.1:1/")
    answers = jev.ask("s", {"q": _question()}, cfg, env=_no_key_env(tmp_path),
                      opener=_boom)
    assert answers["q"].status == "ok"
    assert answers["q"].model == "stub"
    assert answers["q"].reason == "stub (no key)"
    assert "stub" in str(answers["q"])


# --------------------------------------------------------------------------- #
# 6c. sqlite credential store
# --------------------------------------------------------------------------- #

def test_resolve_key_sqlite_readonly(tmp_path):
    db = tmp_path / "opencode.db"
    _make_credential_db(db, [("1", "opencode-go", 1, "sql-secret-1")])
    before = db.read_bytes()
    cfg = _cfg(key_source="auth_store")
    env = dict(_no_key_env(tmp_path), PLANGRAPH_JEV_AUTH_DB=str(db))
    assert jev.resolve_key(cfg, env) == "sql-secret-1"
    assert db.read_bytes() == before  # opened read-only: file untouched


def test_resolve_key_sqlite_wrong_and_inactive_rows(tmp_path):
    cfg = _cfg(key_source="auth_store")
    wrong = tmp_path / "wrong.db"
    _make_credential_db(wrong, [("1", "some-other-integration", 1, "wrong")])
    env = dict(_no_key_env(tmp_path), PLANGRAPH_JEV_AUTH_DB=str(wrong))
    assert jev.resolve_key(cfg, env) is None

    inactive = tmp_path / "inactive.db"
    _make_credential_db(inactive, [("1", "opencode-go", 0, "inactive")])
    env2 = dict(_no_key_env(tmp_path), PLANGRAPH_JEV_AUTH_DB=str(inactive))
    assert jev.resolve_key(cfg, env2) is None

    mixed = tmp_path / "mixed.db"
    _make_credential_db(mixed, [("1", "opencode-go", 0, "inactive"),
                                ("2", "opencode-go", 1, "active-secret")])
    env3 = dict(_no_key_env(tmp_path), PLANGRAPH_JEV_AUTH_DB=str(mixed))
    assert jev.resolve_key(cfg, env3) == "active-secret"


def test_resolve_key_sqlite_json_secret_and_missing_table(tmp_path):
    cfg = _cfg(key_source="auth_store")
    nested = tmp_path / "nested.db"
    _make_credential_db(nested, [("1", "opencode-go", 1,
                                  json.dumps({"key": "nested-secret"}))])
    env = dict(_no_key_env(tmp_path), PLANGRAPH_JEV_AUTH_DB=str(nested))
    assert jev.resolve_key(cfg, env) == "nested-secret"

    empty = tmp_path / "empty.db"
    connection = sqlite3.connect(str(empty))
    connection.execute("CREATE TABLE other (x TEXT)")
    connection.commit()
    connection.close()
    env2 = dict(_no_key_env(tmp_path), PLANGRAPH_JEV_AUTH_DB=str(empty))
    assert jev.resolve_key(cfg, env2) is None

    not_a_db = tmp_path / "not-a-db.db"
    not_a_db.write_text("just text", encoding="utf-8")
    env3 = dict(_no_key_env(tmp_path), PLANGRAPH_JEV_AUTH_DB=str(not_a_db))
    assert jev.resolve_key(cfg, env3) is None


# --------------------------------------------------------------------------- #
# 7. never raises
# --------------------------------------------------------------------------- #

def test_opener_exceptions_become_bug_answers():
    def raise_runtime(*args, **kwargs):
        raise RuntimeError("boom " + SECRET)

    def raise_value(*args, **kwargs):
        raise ValueError("bad value " + SECRET)

    cfg = _cfg(mode="api")
    env = {"JEV_API_KEY": SECRET}
    runtime = jev.ask("s", {"q": _question()}, cfg, env=env, opener=raise_runtime)["q"]
    assert runtime.status == "error"
    assert runtime.reason == "client bug: RuntimeError"
    value = jev.ask("s", {"q": _question()}, cfg, env=env, opener=raise_value)["q"]
    assert value.status == "error"
    assert value.reason == "client bug: ValueError"


def test_keyboard_interrupt_propagates():
    def raise_ki(*args, **kwargs):
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        jev.ask("s", {"q": _question()}, _cfg(mode="api"),
                env={"JEV_API_KEY": SECRET}, opener=raise_ki)


def test_bad_questions_do_not_raise():
    assert jev.ask("s", "not a mapping", _cfg(mode="api"), env={}) == {}
    answers = jev.ask("s", {"q": object()}, _cfg(mode="stub"), env={})
    assert answers["q"].status == "error"


# --------------------------------------------------------------------------- #
# 8. the key never appears
# --------------------------------------------------------------------------- #

def test_key_never_appears_in_answers_or_logs(fake_server, caplog):
    fake_server.status = 500
    fake_server.body = json.dumps(
        {"detail": "Authorization: Bearer " + SECRET}).encode("utf-8")
    cfg = _cfg(url=fake_server.url)
    env = {"JEV_API_KEY": SECRET}
    with caplog.at_level("DEBUG"):
        answers = jev.ask("s", {"q": _question()}, cfg, env=env)
    assert SECRET not in repr(answers)
    assert SECRET not in caplog.text
    assert answers["q"].status == "unavailable"


def test_key_never_appears_from_exception_message():
    def raise_with_key(*args, **kwargs):
        raise RuntimeError("client saw " + SECRET)

    answers = jev.ask("s", {"q": _question()}, _cfg(mode="api"),
                      env={"JEV_API_KEY": SECRET}, opener=raise_with_key)
    assert SECRET not in repr(answers)
    assert answers["q"].reason == "client bug: RuntimeError"


def test_mask_helper():
    assert jev.mask("Bearer " + SECRET + " and " + SECRET,
                    SECRET) == "Bearer *** and ***"
    assert jev.mask("unchanged", "") == "unchanged"
    assert jev.mask(None, SECRET) is None
    assert jev.mask("text", None) == "text"


# --------------------------------------------------------------------------- #
# 9. test-safety guard: the real credential store is never read under pytest
# --------------------------------------------------------------------------- #

@pytest.fixture
def fake_credential_home(tmp_path, monkeypatch):
    """A tmp home/data tree holding a real-shaped fake credential store.

    Every DEFAULT location ``auth_store`` would use is pointed here: a pytest run
    that lost the guard would read this and hand the fake key to ``ask``, which
    is exactly what the guard must make impossible.
    """
    home = tmp_path / "home"
    data = tmp_path / "data"
    local = tmp_path / "local"
    opencode = data / "opencode"
    opencode.mkdir(parents=True)
    (opencode / "auth.json").write_text(
        json.dumps({"opencode": {"type": "api", "key": SECRET}}), encoding="utf-8")
    _make_credential_db(opencode / "opencode.db", [("1", "opencode-go", 1, SECRET)])
    for prefix in (home, local):
        (prefix / "opencode").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    # The marker is what real pytest sets; pin it so the guard is exercised
    # regardless of how this test is invoked.
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "jev guard")
    # An inherited override would lift the guard; the guard tests need it absent.
    monkeypatch.delenv("PLANGRAPH_JEV_AUTH_PATH", raising=False)
    monkeypatch.delenv("PLANGRAPH_JEV_AUTH_DB", raising=False)
    return {"auth": str(opencode / "auth.json"), "db": str(opencode / "opencode.db")}


def test_guard_skips_defaults_but_honours_overrides(fake_credential_home, tmp_path):
    cfg = _cfg(key_source="auth_store")
    reachable = dict(os.environ)  # the fake store IS reachable...
    assert jev.resolve_key(cfg) is None                    # ...but never read
    assert jev.resolve_key(cfg, reachable) is None         # (also via os.environ)
    # An explicit override - what the tests use - still reads the file.
    assert jev.resolve_key(
        cfg, {"PLANGRAPH_JEV_AUTH_PATH": fake_credential_home["auth"]}) == SECRET
    # Per source, independently: the sqlite override alone is enough.
    assert jev.resolve_key(
        cfg, {"PLANGRAPH_JEV_AUTH_DB": fake_credential_home["db"]}) == SECRET
    # An override that names an absent file must NOT fall through to the default.
    absent = str(tmp_path / "absent.json")
    assert jev.resolve_key(cfg, {"PLANGRAPH_JEV_AUTH_PATH": absent}) is None


def test_guard_ask_api_no_override_is_no_key_and_never_opens(fake_credential_home):
    cfg = _cfg(mode="api", url="http://127.0.0.1:1/", key_source="auth_store")
    answers = jev.ask("s", {"q": _question()}, cfg, opener=_boom)
    assert (answers["q"].status, answers["q"].reason) == ("unavailable", "no key")


def test_guard_ask_auto_no_override_returns_stub(fake_credential_home):
    cfg = _cfg(mode="auto", url="http://127.0.0.1:1/", key_source="auth_store")
    answers = jev.ask("s", {"q": _question()}, cfg, opener=_boom)
    assert answers["q"].status == "ok"
    assert answers["q"].model == "stub"
    assert answers["q"].reason == "stub (no key)"
