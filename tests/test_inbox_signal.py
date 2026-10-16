"""RT-P1-INBOX-CODE: the Temporal signal sent when a question is answered.

Part A drives ``temporal_signal.send_answer`` directly against a hermetic fake
Temporal HTTP API: a stdlib ``ThreadingHTTPServer`` on 127.0.0.1 in a thread
that records every request (method, path, headers, JSON body) and answers a
configurable status.  Part B keeps the same fake behind an in-process keeper
and drives ``/v1/ask_question``, ``/v1/answer_question`` and
``/v1/resignal_question`` over HTTP.  Part C needs a real ``temporal`` CLI
(``PLANGRAPH_TEMPORAL_BIN``, else the ``PATH``) and is skipped without it: it
starts ``temporal server start-dev`` on free 127.0.0.1 ports and reads the
signal back out of the workflow history.

``World`` and ``Doors`` (the same helpers the other keeper tests use) keep
every state backend-neutral, so the file runs unchanged under
``PG_KEEPER_BACKEND``.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from door_util import Doors
from keeper_util import World, seed
from plangraph.inbox import temporal_signal

HTTP_ENV = "PLANGRAPH_TEMPORAL_HTTP"
NS_ENV = "PLANGRAPH_TEMPORAL_NAMESPACE"
BIN_ENV = "PLANGRAPH_TEMPORAL_BIN"
#: ``json/plain`` in base64: the Temporal payload encoding metadata.
ENCODING = "anNvbi9wbGFpbg=="
ANSWER_TEXT = "blue"        # the answer the router gives every question
ASK_TEXT = "which branch?"  # the question text
WF = "wf-1"


# --------------------------------------------------------------------- parts
def question(**kw):
    """A ``send_answer`` question dict, the shape ``routes._signal`` builds."""
    base = {"question_id": 7, "key": "demo:A", "text": ASK_TEXT,
            "source": "answer", "answered_at": "2026-10-15T10:00:00Z"}
    base.update(kw)
    return base


def attempt(**kw):
    """A ``store.record_attempt`` attempt dict as the cards tests build one."""
    base = {"harness": "claude", "model": "m-1", "class": "256k",
            "started_at": "2026-10-15T10:00:00Z", "ended_at": "2026-10-15T10:05:00Z",
            "exit_class": "ok", "workflow_id": WF}
    base.update(kw)
    return base


class FakeTemporal:
    """A recording Temporal HTTP API on 127.0.0.1: status and delay are knobs."""

    def __init__(self):
        self.requests = []          # one dict per POST: method, path, headers, body
        self.status = 200           # answered status
        self.delay = 0.0            # seconds spent before answering
        self._closed = False
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - stdlib signature
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw.decode("utf-8")) if raw else None
                except ValueError:
                    body = None
                outer.requests.append({
                    "method": "POST", "path": self.path, "body": body,
                    "headers": {k.lower(): v for k, v in self.headers.items()}})
                if outer.delay:
                    time.sleep(outer.delay)
                reply = b'{"ok":true}'
                try:
                    self.send_response(outer.status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(reply)))
                    self.end_headers()
                    self.wfile.write(reply)
                except OSError:      # the client gave up before the late reply
                    pass

            def log_message(self, *args):
                pass                 # never print a request line

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def payload_of(request):
    """The decoded ``{question_id, key, text, source}`` of one recorded POST."""
    payload = request["body"]["input"]["payloads"][0]
    assert payload["metadata"]["encoding"] == ENCODING
    return json.loads(base64.b64decode(payload["data"]))


def assert_no_leak(out, base):
    """A status dict carries only the documented keys and no URL/body text."""
    assert set(out) == {"status", "workflow_id"}
    blob = json.dumps(out, sort_keys=True)
    for absent in (base, "/api/v1", "signal/answer", "plangraph-q", ASK_TEXT):
        assert absent not in blob, blob


# ----------------------------------------------------------------- fixtures
@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    try:
        yield world
    finally:
        world.close()


@pytest.fixture
def fake():
    server = FakeTemporal()
    try:
        yield server
    finally:
        server.close()


@pytest.fixture
def env_fake(monkeypatch, fake):
    """The fake Temporal API configured as the keeper's signal target."""
    monkeypatch.setenv(HTTP_ENV, fake.base)
    monkeypatch.delenv(NS_ENV, raising=False)
    return fake


@pytest.fixture
def doors(w, tmp_path, env_fake):
    live = Doors(w, tmp_path)
    try:
        yield live
    finally:
        live.close()


def ask(doors, key, text=ASK_TEXT):
    """The id of a fresh open question on *key*, asked over HTTP as router."""
    status, body = doors.http("router", "POST", "/v1/ask_question",
                              {"key": key, "text": text})
    assert status == 200, body
    return body["id"]


# ------------------------------------------------- Part A: send_answer
@pytest.mark.parametrize("raw", [None, "", "ftp://x", "not a url"])
def test_disabled_config_sends_nothing(monkeypatch, fake, raw):
    if raw is None:
        monkeypatch.delenv(HTTP_ENV, raising=False)
    else:
        monkeypatch.setenv(HTTP_ENV, raw)
    assert temporal_signal.send_answer(question(workflow_id=WF)) == {
        "status": "disabled", "workflow_id": None}
    assert fake.requests == []


def test_signal_path_payload_identity_and_request_id(monkeypatch, fake):
    monkeypatch.delenv(NS_ENV, raising=False)
    monkeypatch.setenv(HTTP_ENV, fake.base)
    out = temporal_signal.send_answer(question(workflow_id=WF))
    assert out == {"status": "sent", "workflow_id": WF}
    assert [r["method"] for r in fake.requests] == ["POST"]
    seen = fake.requests[0]
    assert seen["path"] == "/api/v1/namespaces/default/workflows/wf-1/signal/answer"
    body = seen["body"]
    assert body["identity"] == "plangraph-keeper"
    assert body["requestId"] == "plangraph-q7-answer"
    assert payload_of(seen) == {"question_id": 7, "key": "demo:A",
                                "text": ASK_TEXT, "source": "answer"}


def test_namespace_and_odd_workflow_id_are_in_the_encoded_path(monkeypatch, fake):
    monkeypatch.setenv(HTTP_ENV, fake.base)
    monkeypatch.setenv(NS_ENV, "ns1")
    out = temporal_signal.send_answer(question(workflow_id="wf/one two"))
    assert out == {"status": "sent", "workflow_id": "wf/one two"}
    assert fake.requests[0]["path"] == (
        "/api/v1/namespaces/ns1/workflows/wf%2Fone%20two/signal/answer")


@pytest.mark.parametrize("status,expected", [(404, "not_found"), (500, "failed")])
def test_error_statuses_do_not_raise_or_leak(monkeypatch, fake, status, expected):
    monkeypatch.setenv(HTTP_ENV, fake.base)
    fake.status = status
    out = temporal_signal.send_answer(question(workflow_id=WF))
    assert out == {"status": expected, "workflow_id": WF}
    assert len(fake.requests) == 1
    assert_no_leak(out, fake.base)


def test_connection_refused_is_failed_and_leaks_nothing(monkeypatch, fake):
    base = fake.base
    fake.close()                      # nothing listens on that port any more
    monkeypatch.setenv(HTTP_ENV, base)
    out = temporal_signal.send_answer(question(workflow_id=WF))
    assert out == {"status": "failed", "workflow_id": WF}
    assert_no_leak(out, base)


def test_timeout_is_failed_quickly_and_leaks_nothing(monkeypatch, fake):
    monkeypatch.setenv(HTTP_ENV, fake.base)
    monkeypatch.setattr(temporal_signal, "TIMEOUT_S", 0.3)
    fake.delay = 1.5                   # the server answers long after the deadline
    started = time.monotonic()
    out = temporal_signal.send_answer(question(workflow_id=WF))
    assert out == {"status": "failed", "workflow_id": WF}
    assert time.monotonic() - started < fake.delay
    assert len(fake.requests) == 1     # the request was made, the reply was late
    assert_no_leak(out, fake.base)


def test_timeout_source_is_skipped_and_sends_nothing(monkeypatch, fake):
    monkeypatch.setenv(HTTP_ENV, fake.base)
    out = temporal_signal.send_answer(question(workflow_id=WF,
                                               source="question_timeout"))
    assert out == {"status": "skipped_timeout", "workflow_id": WF}
    assert fake.requests == []


def test_no_attempt_means_no_workflow(monkeypatch, fake, w):
    monkeypatch.setenv(HTTP_ENV, fake.base)
    (key,) = seed(w, [("A", [])])
    out = temporal_signal.send_answer(question(key=key), w.conn)
    assert out == {"status": "no_workflow", "workflow_id": None}
    assert fake.requests == []


def test_workflow_id_comes_from_the_newest_attempt_that_has_one(monkeypatch, fake, w):
    monkeypatch.setenv(HTTP_ENV, fake.base)
    monkeypatch.delenv(NS_ENV, raising=False)
    (key,) = seed(w, [("A", [])])
    router = w.p("router")
    w.store.record_attempt(router, key, attempt(n=1, workflow_id="wf-old"))
    w.store.record_attempt(router, key, attempt(n=2, workflow_id=None))
    out = temporal_signal.send_answer(question(key=key), w.conn)
    assert out == {"status": "sent", "workflow_id": "wf-old"}
    assert fake.requests[0]["path"].endswith("/workflows/wf-old/signal/answer")


# ----------------------------------------------- Part B: keeper over HTTP
def test_answer_and_resignal_each_send_the_signal(doors, fake, w):
    (key,) = seed(w, [("A", [])])
    w.store.record_attempt(w.p("router"), key, attempt(workflow_id=WF))
    qid = ask(doors, key)
    status, answered = doors.http("router", "POST", "/v1/answer_question",
                                  {"question_id": qid, "text": ANSWER_TEXT})
    assert status == 200, answered
    assert answered["state"] == "answered" and answered["source"] == "answer"
    assert answered["signal"] == {"status": "sent", "workflow_id": WF}
    assert len(fake.requests) == 1
    first = fake.requests[0]
    assert first["path"] == "/api/v1/namespaces/default/workflows/wf-1/signal/answer"
    assert payload_of(first)["text"] == ANSWER_TEXT

    status, again = doors.http("router", "POST", "/v1/resignal_question",
                               {"question_id": qid})
    assert status == 200, again
    assert again["signal"] == {"status": "sent", "workflow_id": WF}
    assert len(fake.requests) == 2
    second = fake.requests[1]
    assert second["body"]["requestId"] == first["body"]["requestId"]
    assert payload_of(second) == payload_of(first)


def test_question_whose_item_has_no_attempt_reports_no_workflow(doors, fake, w):
    tried, waiting = seed(w, [("A", []), ("B", [])])
    w.store.record_attempt(w.p("router"), tried, attempt(workflow_id=WF))
    qid = ask(doors, waiting)
    status, answered = doors.http("router", "POST", "/v1/answer_question",
                                  {"question_id": qid, "text": ANSWER_TEXT})
    assert status == 200, answered
    assert answered["signal"] == {"status": "no_workflow", "workflow_id": None}
    assert fake.requests == []


def test_signal_failure_never_rolls_back_the_answer(doors, fake, w):
    fake.status = 500
    (key,) = seed(w, [("A", [])])
    w.store.record_attempt(w.p("router"), key, attempt(workflow_id=WF))
    qid = ask(doors, key)
    status, answered = doors.http("router", "POST", "/v1/answer_question",
                                  {"question_id": qid, "text": ANSWER_TEXT})
    assert status == 200, answered
    assert answered["state"] == "answered"
    assert answered["signal"] == {"status": "failed", "workflow_id": WF}
    assert len(fake.requests) == 1

    status, listed = doors.http("router", "GET", "/v1/questions?state=answered")
    assert status == 200, listed
    assert [row["id"] for row in listed["questions"]] == [qid]
    row = listed["questions"][0]
    assert row["state"] == "answered"
    assert row["answer"]["text"] == ANSWER_TEXT and row["answer"]["actor"] == "router"


# --------------------------------------------- Part C: the real dev server
def temporal_cli():
    """The Temporal CLI from the environment, else the ``PATH``, else ``None``."""
    return os.environ.get(BIN_ENV, "").strip() or shutil.which("temporal")


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def cli(argv, timeout=60):
    """One Temporal CLI call, always with a hard timeout; never through a shell."""
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


@pytest.fixture(scope="module")
def dev():
    binary = temporal_cli()
    if not binary:
        pytest.skip(f"no temporal CLI: {BIN_ENV} is unset and 'temporal' is not on PATH")
    port, http_port = free_port(), free_port()
    proc = subprocess.Popen(
        [binary, "server", "start-dev", "--headless", "--ip", "127.0.0.1",
         "--port", str(port), "--http-port", str(http_port), "--log-level", "error"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{http_port}"
    try:
        deadline = time.time() + 60
        ready = False
        while time.time() < deadline and proc.poll() is None:
            try:
                with urllib.request.urlopen(
                        base + "/api/v1/namespaces/default", timeout=2) as resp:
                    ready = resp.status == 200
            except (urllib.error.URLError, OSError):
                ready = False
            if ready:
                break
            time.sleep(0.5)
        if not ready:
            pytest.fail(f"temporal dev server not ready within 60s (exit {proc.poll()})")
        yield SimpleNamespace(binary=binary, port=port, http_port=http_port, base=base)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


def history_events(node):
    """Every ``eventType`` dict anywhere in a CLI ``--output json`` document."""
    if isinstance(node, dict):
        if isinstance(node.get("eventType"), str):
            yield node
        for value in node.values():
            yield from history_events(value)
    elif isinstance(node, list):
        for item in node:
            yield from history_events(item)


def test_signal_arrives_in_a_real_workflow_history(dev, w, tmp_path, monkeypatch):
    started = cli([dev.binary, "workflow", "start", "--address",
                   f"127.0.0.1:{dev.port}", "--task-queue", "q", "--type", "Card",
                   "--workflow-id", "wf-it", "--input", "{}"])
    assert started.returncode == 0, (started.stdout, started.stderr)
    monkeypatch.setenv(HTTP_ENV, dev.base)
    monkeypatch.delenv(NS_ENV, raising=False)
    (key,) = seed(w, [("A", [])])
    w.store.record_attempt(w.p("router"), key, attempt(workflow_id="wf-it"))
    doors = Doors(w, tmp_path)
    try:
        qid = ask(doors, key)
        status, answered = doors.http("router", "POST", "/v1/answer_question",
                                      {"question_id": qid, "text": ANSWER_TEXT})
    finally:
        doors.close()
    assert status == 200, answered
    assert answered["signal"] == {"status": "sent", "workflow_id": "wf-it"}

    shown = cli([dev.binary, "workflow", "show", "--address",
                 f"127.0.0.1:{dev.port}", "-w", "wf-it", "--output", "json"])
    assert shown.returncode == 0, (shown.stdout, shown.stderr)
    signalled = [e for e in history_events(json.loads(shown.stdout))
                 if e.get("eventType") == "EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED"]
    assert signalled, "no EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED in the history"
    attrs = signalled[0]["workflowExecutionSignaledEventAttributes"]
    assert attrs["signalName"] == "answer"
    payload = attrs["input"]["payloads"][0]
    assert json.loads(base64.b64decode(payload["data"]))["text"] == ANSWER_TEXT


def test_answer_to_an_unknown_workflow_is_not_found(dev, w, tmp_path, monkeypatch):
    monkeypatch.setenv(HTTP_ENV, dev.base)
    monkeypatch.delenv(NS_ENV, raising=False)
    (key,) = seed(w, [("A", [])])
    w.store.record_attempt(w.p("router"), key, attempt(workflow_id="wf-missing"))
    doors = Doors(w, tmp_path)
    try:
        qid = ask(doors, key)
        status, answered = doors.http("router", "POST", "/v1/answer_question",
                                      {"question_id": qid, "text": ANSWER_TEXT})
    finally:
        doors.close()
    assert status == 200, answered
    assert answered["signal"] == {"status": "not_found",
                                  "workflow_id": "wf-missing"}
