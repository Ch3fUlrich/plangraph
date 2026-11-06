"""Hermetic S2a HTTP snapshot contract and private validation."""
import json

import pytest

from keeper_util import World, seed, to_done
from test_keeper_api import srv, call  # noqa: F401
from plangraph.keeper import fmt, timing
from plangraph.keeper.snapshot_params import parse


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path, backend="sqlite")
    keys = seed(world, [("A", []), ("B", [])])
    to_done(world, keys[1])
    yield world
    world.close()


@pytest.mark.parametrize("query,param,sentinel", [
    ("open=0", "open", "0"),
    ("closed=PRIVATE", "closed", "PRIVATE"),
    ("open=1&open=PRIVATE", "open", "PRIVATE"),
    ("fmt=json&fmt=PRIVATE", "fmt", "PRIVATE"),
    ("fields=title,title", "fields", "title"),
    ("blocks=items,items", "blocks", "items"),
    ("since=2024-01-01T00:00:00%2B00:00", "since", "2024-01-01"),
    ("since=2024-02-30T00:00:00Z", "since", "2024-02-30"),
    ("since=PRIVATE", "since", "PRIVATE"),
    ("open=1&closed=1", "open", "1"),
    ("fields=PRIVATE", "fields", "PRIVATE"),
    ("blocks=PRIVATE", "blocks", "PRIVATE"),
    ("open=1&fmt=text", "fmt", "text"),
])
def test_400_private(w, srv, capsys, query, param, sentinel):
    status, _, body = call(srv, "/v1/snapshot?" + query, token=w.tok("reader"))
    assert status == 400
    data = json.loads(body)
    assert data["error"] == "validation" and param in data["msg"]
    assert sentinel not in body.decode()
    assert sentinel not in capsys.readouterr().err


@pytest.mark.parametrize("query", ["", "fmt=json", "fmt=text"])
def test_legacy_bytes(w, srv, query):
    data = w.store.snapshot(w.p("reader"))
    expected = (fmt.snapshot_text(data) if query == "fmt=text" else json.dumps(data)).encode()
    status, _, body = call(srv, "/v1/snapshot?" + query, token=w.tok("reader"))
    assert status == 200 and body == expected
    assert b'"filter"' not in body


@pytest.mark.parametrize("name", ["open", "closed", "since", "fields", "blocks", "fmt"])
def test_blank_omitted(w, srv, name):
    baseline = call(srv, "/v1/snapshot", token=w.tok("reader"))
    assert call(srv, "/v1/snapshot?" + name + "=", token=w.tok("reader")) == baseline


@pytest.mark.parametrize("name", ["open", "closed", "since", "fields", "blocks", "fmt"])
def test_parse_blank_values(name):
    assert parse({name: ["", ""]}) == parse({})
    value = {"open": "1", "closed": "1", "since": "2024-01-01T00:00:00Z",
             "fields": "title", "blocks": "items", "fmt": "json"}[name]
    assert parse({name: ["", value]}) == parse({name: [value]})


@pytest.mark.parametrize("query", ["open=1", "closed=1", "fields=title", "blocks=items",
                                   "since=2024-01-01T00:00:00.1Z", ""])
def test_echo_and_timing(w, srv, monkeypatch, capsys, query):
    monkeypatch.setattr(timing, "THRESHOLD_MS", 0)
    status, _, body = call(srv, "/v1/snapshot?" + query, token=w.tok("reader"))
    assert status == 200
    assert ("filter" in json.loads(body)) == bool(query)
    lines = capsys.readouterr().err.splitlines()
    request = next(line for line in lines if "request_timing" in line)
    assert ("filtered=1" in request) == bool(query)
