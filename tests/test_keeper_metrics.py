"""Keeper metrics verbs: set_metrics, get_metrics and log_governor_override.

Portable: uses the same ``World`` fixture as ``tests/test_keeper_cards.py`` so the
Postgres CI job exercises the identical assertions.  Rows are read back through
``get_metrics`` or ``w.conn.execute`` only.
"""
import inspect
import sqlite3
from types import SimpleNamespace

import pytest

from keeper_util import World
from plangraph.keeper import api_routes, schema
from plangraph.keeper.errors import Refused


DAY = "2026-10-16"
NEED = lambda body, *names: body[names[0]]


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    yield world
    world.close()


def refused(code, fn, *args, **kw):
    with pytest.raises(Refused) as info:
        fn(*args, **kw)
    assert info.value.code == code, info.value.msg
    return info.value


def count(w, table):
    return w.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def item(**kw):
    base = {"metric": "latency", "value": 1.0}
    base.update(kw)
    return base


def row(w, day, metric):
    return w.store.get_metrics(w.p("router"), day=day, metric=metric)["rows"][0]


def gov_rows(w):
    return w.conn.execute(
        "SELECT action, ok, msg, code FROM audit_log WHERE action='governor_override'"
    ).fetchall()


def _record(name):
    """A store stub whose ``name`` lambda records every call it receives."""
    calls = []
    def fn(*args, **kwargs):
        calls.append((args, kwargs))
    return SimpleNamespace(**{name: fn}), calls


def _values(call):
    args, kwargs = call
    return list(args) + list(kwargs.values())


def _arity(fn):
    sig = inspect.signature(fn)
    if any(p.kind == p.VAR_POSITIONAL for p in sig.parameters.values()):
        return None
    return len(sig.parameters)


def _call(route, store, principal, body, need):
    """Invoke a router-verb with the (store, principal, body, need) or
    (store, body, need) shape; ``body`` carries the principal too so both work."""
    n = _arity(route)
    if n == 3:
        return route(store, body, need)
    return route(store, principal, body, need)


# --- set_metrics: upsert + batch + roles -------------------------------------

def test_set_metrics_upsert_is_idempotent_per_day_metric(w):
    router = w.p("router")
    res = w.store.set_metrics(router, DAY, [item(source="run1")])
    assert res == {"day": DAY, "count": 1}
    assert count(w, "metrics") == 1
    first = row(w, DAY, "latency")
    assert first["value"] == 1.0
    assert first["source"] == "run1"
    w.clock.advance(60)
    res = w.store.set_metrics(router, DAY, [item(value=2.5, source="run2")])
    assert res == {"day": DAY, "count": 1}
    assert count(w, "metrics") == 1
    second = row(w, DAY, "latency")
    assert second["value"] == 2.5
    assert second["source"] == "run2"
    assert second["computed_at"] != first["computed_at"]


def test_set_metrics_batch_writes_all_and_system_may_write(w):
    system = w.p("system")
    res = system_store = w.store.set_metrics(system, DAY, [
        {"metric": "a", "value": 1},
        {"metric": "b", "value": 2, "source": "s"},
        {"metric": "c", "value": 3},
    ])
    assert res == {"day": DAY, "count": 3}
    assert count(w, "metrics") == 3


@pytest.mark.parametrize("name", ["l1main", "workerA", "reviewer", "reader"])
def test_set_metrics_forbidden(w, name):
    refused("forbidden", w.store.set_metrics, w.p(name), DAY, [item()])
    assert count(w, "metrics") == 0


# --- set_metrics: validation matrix ------------------------------------------

@pytest.mark.parametrize("bad_day", ["15.10.2026", "2026-13-40", None, "2026-10-16T00:00", "2026-10"])
def test_set_metrics_rejects_bad_day(w, bad_day):
    refused("validation", w.store.set_metrics, w.p("router"), bad_day, [item()])


@pytest.mark.parametrize("bad_value", [True, False, float("nan"), float("inf"),
                                       -float("inf"), "x", None, [1]])
def test_set_metrics_rejects_bad_value(w, bad_value):
    refused("validation", w.store.set_metrics, w.p("router"), DAY, [item(value=bad_value)])


@pytest.mark.parametrize("bad_metric", ["", "a b", "a" * 65, None, "with/slash"])
def test_set_metrics_rejects_bad_metric(w, bad_metric):
    refused("validation", w.store.set_metrics, w.p("router"), DAY, [item(metric=bad_metric)])


@pytest.mark.parametrize("bad_source", ["a" * 65, 5, ["x"], {"s": 1}])
def test_set_metrics_rejects_bad_source(w, bad_source):
    refused("validation", w.store.set_metrics, w.p("router"), DAY, [item(source=bad_source)])


@pytest.mark.parametrize("bad_items", [
    [],
    "not-a-list",
    {"metric": "x", "value": 1},
    ["a"],
    [1],
    [{"metric": "latency", "value": 1}] * 51,
])
def test_set_metrics_rejects_bad_items(w, bad_items):
    refused("validation", w.store.set_metrics, w.p("router"), DAY, bad_items)


def test_set_metrics_mixed_batch_writes_nothing(w):
    router = w.p("router")
    items = [item(metric="good", value=1.0), item(metric="bad name", value="oops")]
    refused("validation", w.store.set_metrics, router, DAY, items)
    assert count(w, "metrics") == 0


def test_set_metrics_accepts_null_and_64_char_source(w):
    router = w.p("router")
    res = w.store.set_metrics(router, DAY, [
        {"metric": "a", "value": 1, "source": None},
        {"metric": "b", "value": 1, "source": "s" * 64},
    ])
    assert res == {"day": DAY, "count": 2}
    assert row(w, DAY, "a")["source"] == ""


# --- get_metrics -------------------------------------------------------------

def test_get_metrics_reader_may_read_and_empty_result(w):
    assert w.store.get_metrics(w.p("reader")) == {"rows": []}


def test_get_metrics_filters_and_orders_by_day_metric(w):
    router = w.p("router")
    reader = w.p("reader")
    w.store.set_metrics(router, DAY, [{"metric": "b", "value": 2}, {"metric": "a", "value": 1}])
    w.store.set_metrics(router, "2026-10-15", [{"metric": "c", "value": 3}])
    allrows = w.store.get_metrics(reader)["rows"]
    assert [(r["day"], r["metric"]) for r in allrows] == [
        ("2026-10-15", "c"), (DAY, "a"), (DAY, "b")]
    assert {r["metric"] for r in w.store.get_metrics(reader, day=DAY)["rows"]} == {"a", "b"}
    assert {r["metric"] for r in w.store.get_metrics(reader, since=DAY)["rows"]} == {"a", "b"}
    only = w.store.get_metrics(reader, day=DAY, metric="a")["rows"]
    assert len(only) == 1 and only[0]["value"] == 1.0


def test_get_metrics_row_shape_and_value_is_float(w):
    router = w.p("router")
    w.store.set_metrics(router, DAY, [item(value=7, source="ci")])
    r = row(w, DAY, "latency")
    assert set(r.keys()) == {"day", "metric", "value", "source", "computed_at"}
    assert isinstance(r["value"], float) and r["value"] == 7.0


@pytest.mark.parametrize("kw", [
    {"day": "bad"}, {"since": "2026-13-40"}, {"metric": "a b"}, {"metric": "a" * 65},
])
def test_get_metrics_rejects_bad_args(w, kw):
    refused("validation", w.store.get_metrics, w.p("reader"), **kw)


# --- log_governor_override ---------------------------------------------------

def test_spawner_role_not_exercised():
    pytest.skip("no spawner principal in the World fixture; _log_governor_override allows it")


@pytest.mark.parametrize("name", ["router", "l1main", "system"])
def test_log_governor_override_success_audits(w, name):
    assert w.store.log_governor_override(w.p(name), "planner", "operator", "manual start") \
        == {"logged": True}
    rows = gov_rows(w)
    assert len(rows) == 1
    assert rows[0]["ok"] == 1
    assert "manual start" in rows[0]["msg"] and "planner" in rows[0]["msg"]


def test_log_governor_override_msg_includes_provider_or_dash(w):
    router = w.p("router")
    w.store.log_governor_override(router, "expert", "agent", "because", provider="openai")
    msg = gov_rows(w)[0]["msg"]
    for frag in ("expert", "agent", "openai", "because"):
        assert frag in msg
    w.store.log_governor_override(router, "judge", "operator", "no provider")
    assert "-" in gov_rows(w)[-1]["msg"]


@pytest.mark.parametrize("name", ["workerA", "reader", "reviewer"])
def test_log_governor_override_forbidden(w, name):
    refused("forbidden", w.store.log_governor_override, w.p(name), "planner", "operator", "r")
    assert len(gov_rows(w)) == 0
    rows = w.conn.execute(
        "SELECT action, ok, code FROM audit_log "
        "WHERE action='log_governor_override' AND ok=0").fetchall()
    assert rows and all(r["code"] == "forbidden" for r in rows)


@pytest.mark.parametrize("start_kind,initiator,reason,provider", [
    ("bogus", "operator", "r", None),
    ("planner", "robot", "r", None),
    ("planner", "operator", "", None),
    ("planner", "operator", "   ", None),
    ("planner", "operator", None, None),
    ("planner", "operator", "x" * 501, None),
    ("planner", "operator", "r", "bad provider"),
    ("planner", "operator", "r", "a" * 65),
])
def test_log_governor_override_validation(w, start_kind, initiator, reason, provider):
    refused("validation", w.store.log_governor_override, w.p("router"),
            start_kind, initiator, reason, provider)
    assert len(gov_rows(w)) == 0


# --- api route tables --------------------------------------------------------

def test_api_route_membership():
    assert "metrics" in api_routes.READ_VERBS
    assert "set_metrics" in api_routes.WRITE_VERBS
    assert "governor_override" in api_routes.WRITE_VERBS
    assert "set_metrics" in api_routes.ROLE_CHECKED_VERBS
    assert "governor_override" in api_routes.ROLE_CHECKED_VERBS


def test_route_set_metrics_passes_day_and_items_through(w):
    store, calls = _record("set_metrics")
    body = {"principal": w.p("router"), "day": DAY, "items": [item()]}
    _call(api_routes.ROLE_CHECKED_VERBS["set_metrics"], store, body["principal"], body, NEED)
    vals = _values(calls[0])
    assert body["day"] in vals
    assert body["items"] in vals


def test_route_governor_override_passes_through_default_provider_none(w):
    store, calls = _record("log_governor_override")
    body = {"principal": w.p("router"), "start_kind": "planner",
            "initiator": "operator", "reason": "go"}
    _call(api_routes.ROLE_CHECKED_VERBS["governor_override"], store, body["principal"], body, NEED)
    vals = _values(calls[0])
    assert "planner" in vals and "operator" in vals and "go" in vals
    assert None in vals  # provider defaulted to None


def test_route_governor_override_passes_provider(w):
    store, calls = _record("log_governor_override")
    body = {"principal": w.p("router"), "start_kind": "answer",
            "initiator": "agent", "reason": "go", "provider": "openai"}
    _call(api_routes.ROLE_CHECKED_VERBS["governor_override"], store, body["principal"], body, NEED)
    assert "openai" in _values(calls[0])


# --- schema ------------------------------------------------------------------

def test_schema_version_and_metrics_ddl():
    assert schema.SCHEMA_VERSION == 16
    assert schema.METRICS_DDL in schema.SCHEMA


def test_fresh_database_has_metrics_table_with_ordered_columns(w):
    if not isinstance(w.conn, sqlite3.Connection):
        pytest.skip("PRAGMA table_info is SQLite-specific")
    cols = [r[1] for r in w.conn.execute("PRAGMA table_info(metrics)")]
    assert cols == ["day", "metric", "value", "source", "computed_at"]

