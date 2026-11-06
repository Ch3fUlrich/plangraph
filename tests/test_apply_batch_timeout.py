"""Mutation budgets and the operator barrier after an uncertain response."""
import io
import json
import urllib.error

import pytest

from test_apply_batch import apply_batch, write_batch, run


class Reply:
    def __init__(self, raw):
        self.raw = raw

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self):
        if isinstance(self.raw, Exception):
            raise self.raw
        return self.raw


@pytest.fixture
def fake(tmp_path, monkeypatch):
    class Keeper:
        calls = None
        mode = "ok"
        applied = False

        def __init__(self):
            self.calls = []

        def open(self, req, timeout):
            self.calls.append((req, timeout))
            if req.method == "GET":
                return Reply(b'{"name":"router","role":"router"}')
            if self.mode == "refusal":
                raise urllib.error.HTTPError(req.full_url, 409, "", {},
                                            io.BytesIO(b'{"error":"conflict"}'))
            self.applied = self.mode != "not-applied"
            if self.mode in ("applied", "not-applied"):
                raise TimeoutError()
            if self.mode == "read":
                return Reply(ConnectionResetError())
            if self.mode == "short":
                return Reply(b'{"applied":0}')
            if self.mode == "malformed":
                return Reply(b'broken')
            body = json.loads(req.data)
            return Reply(json.dumps({"applied": len(body.get("ops", [1]))}).encode())

    keeper = Keeper()
    monkeypatch.setattr(apply_batch.urllib.request, "build_opener", lambda *a: keeper)
    token = tmp_path / "credential"
    token.write_text("private-credential")
    batch = write_batch(tmp_path, ops=[{"op": "cancel", "key": "demo:A"}])
    return keeper, batch, str(token)


def invoke(fake, extra=()):
    keeper, batch, token = fake
    return run(batch, "http://unused.invalid", token, extra)


def test_default_budget(fake):
    assert invoke(fake)[0] == 0
    assert fake[0].calls[-1][1] == 180


def test_applied_then_timeout(fake):
    fake[0].mode = "applied"
    rc, out, err = invoke(fake)
    assert rc != 0
    assert "unknown outcome" in out, out
    assert "nothing applied" not in out


@pytest.mark.parametrize("extra,budget", [([], 180), (["--timeout", "240.5"], 240.5)])
@pytest.mark.parametrize("per_op", [False, True])
def test_budget_and_slow_completion(fake, extra, budget, per_op):
    import yaml
    doc = yaml.safe_load(fake[1].read_text())
    from test_apply_batch import LANE_B
    doc["ops"] = [{"op": "create", "item": [dict(LANE_B, id=f"C{i}", title="x" * 1000)
                                           for i in range(100)]}]
    fake[1].write_text(yaml.safe_dump(doc))
    original = fake[0].open
    def slow(req, timeout):
        # Simulated 100-second completion: old 15-second budget would expire.
        if req.method == "POST" and timeout < 100:
            raise TimeoutError()
        return original(req, timeout)
    fake[0].open = slow
    assert invoke(fake, [*extra, *(["--per-op"] if per_op else [])])[0] == 0
    assert [t for r, t in fake[0].calls if r.method == "POST"] == [budget]
    assert fake[0].calls[0][1] == 15


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "-inf", "bad"])
def test_invalid_timeout_before_calls(fake, value):
    assert invoke(fake, ["--timeout=" + value])[0] != 0
    assert not fake[0].calls


@pytest.mark.parametrize("problem", ["op", "pin", "encoding"])
def test_late_preflight_before_calls(fake, problem):
    import yaml
    doc = yaml.safe_load(fake[1].read_text())
    if problem == "pin":
        doc["source_sha256"] = "bad"
    else:
        doc["ops"].append({"op": "edit", "key": "demo:B",
                           "fields": {"title": float("nan")}} if problem == "encoding"
                          else {"op": "invalid"})
    fake[1].write_text(yaml.safe_dump(doc))
    assert invoke(fake)[0] != 0
    assert not fake[0].calls


@pytest.mark.parametrize("mode", ["applied", "not-applied", "read", "short", "malformed"])
@pytest.mark.parametrize("per_op", [False, True])
def test_unknown_barrier(fake, mode, per_op, tmp_path):
    fake[0].mode = mode
    queue = tmp_path / "queue"
    extra = ["--queue-line", "pending", "--queue-file", str(queue)]
    if per_op:
        extra.append("--per-op")
    rc, out, err = invoke(fake, extra)
    assert rc != 0
    assert "unknown outcome" in out
    assert "reconcile this exact operation against keeper history and current state before any retry; do not blindly rerun" in out
    assert "leave outcome unknown and do not retry" in out
    assert "index 1" in out if per_op else "slice 1-1" in out
    assert "nothing applied" not in out
    assert "resume with" not in out
    assert "summary:" not in out
    assert "private-credential" not in out + err
    assert "unused.invalid" not in out + err
    assert not queue.exists()
    assert len([r for r, t in fake[0].calls if r.method == "POST"]) == 1
    import apply_batch_transport as transport
    request = fake[0].calls[-1][0]
    import yaml
    doc = yaml.safe_load(fake[1].read_text())
    identity = transport.operation_identity("router", request.full_url.rsplit("/", 1)[-1],
                                          json.loads(request.data), doc["source_sha256"],
                                          "index 1" if per_op else "slice 1-1")
    assert identity in out
    assert fake[0].applied == (mode != "not-applied")


def test_identity_binding():
    from apply_batch_transport import operation_identity as fingerprint
    args = ["router", "cancel", {"key": "demo:A", "reason": "x"}, "digest", "index 1"]
    original = fingerprint(*args)
    assert fingerprint("router", "cancel", {"reason": "x", "key": "demo:A"}, "digest", "index 1") == original
    for index, value in enumerate(["other", "edit", {"key": "demo:B"}, "other-digest", "index 2"]):
        changed = args.copy()
        changed[index] = value
        assert fingerprint(*changed) != original


def test_explicit_refusal(fake):
    fake[0].mode = "refusal"
    rc, out, err = invoke(fake)
    assert rc != 0
    assert "nothing applied (the whole batch was rolled back)" in out
    assert "unknown outcome" not in out


def test_snapshot_budget(fake):
    session = apply_batch.Session("http://unused.invalid", "private-credential")
    session.snapshot()
    assert fake[0].calls[-1][1] == 60


@pytest.mark.parametrize("raw", [b"conflict", b'{"result":{}}'])
def test_http_refusal_without_error_field(fake, raw):
    def refuse(req, timeout):
        if req.method == "GET":
            return Reply(b'{"name":"router","role":"router"}')
        raise urllib.error.HTTPError(req.full_url, 409, "", {}, io.BytesIO(raw))
    fake[0].open = refuse
    rc, out, err = invoke(fake)
    assert rc != 0
    assert "409" in out
    assert "nothing applied (the whole batch was rolled back)" in out
    assert "unknown outcome" not in out


def test_per_op_apply_ops_without_applied(fake, monkeypatch):
    original = fake[0].open
    def without_count(req, timeout):
        if req.method == "POST":
            assert req.full_url.endswith("/apply_ops")
            return Reply(b'{"result":{}}')
        return original(req, timeout)
    fake[0].open = without_count
    monkeypatch.setattr(apply_batch, "build_request", lambda *a: ("apply_ops", {}))
    rc, out, err = invoke(fake, ["--per-op"])
    assert rc == 0
    assert "ok 1 cancel demo:A" in out
    assert "unknown outcome" not in out


def test_per_op_partial_success(fake):
    import yaml
    doc = yaml.safe_load(fake[1].read_text())
    doc["ops"] *= 2
    fake[1].write_text(yaml.safe_dump(doc))
    original = fake[0].open
    def second_lost(req, timeout):
        if len(fake[0].calls) == 2:
            fake[0].mode = "applied"
        return original(req, timeout)
    fake[0].open = second_lost
    rc, out, err = invoke(fake, ["--per-op"])
    assert rc != 0
    assert "ok 1 cancel demo:A" in out
    assert "unknown outcome" in out and "index 2" in out
    assert "resume with" not in out and "summary:" not in out
