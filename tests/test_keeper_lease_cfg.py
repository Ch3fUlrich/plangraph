"""SU-LEASE-CFG: daemon construction applies loaded lease and alert settings."""
import pytest

from keeper_util import seed, to_in_review
from test_keeper_config import write_config
from plangraph.keeper import config, daemon
from plangraph.keeper.store import DEFAULT_LEASE_S, LEASE_WARN_S, REVIEW_OVERDUE_S


@pytest.fixture
def configured_store(w, tmp_path):
    def build(data):
        cfg = config.load_config(write_config(tmp_path, data))
        store = daemon._make_store(w.conn, cfg)
        store.clock = w.clock
        store.verifiers = w.verifiers
        w.store = store
        return store
    return build


@pytest.mark.parametrize("data, seconds", [({"default_lease": 600}, 600), ({}, 7200)])
def test_daemon_config_sets_new_claim_lease(w, configured_store, data, seconds):
    store = configured_store(data)
    (key,) = seed(w, [("A", [])])
    out = store.claim(w.p("l1main"), key)
    expected = store._iso(w.clock() + seconds)
    assert out["lease_until"] == expected
    assert store.show(w.p("reader"), key)["lease_until"] == expected


def test_absent_settings_keep_config_and_store_defaults(configured_store, tmp_path):
    cfg = config.load_config(write_config(tmp_path, {}))
    store = configured_store({})
    assert cfg.default_lease == store.default_lease == DEFAULT_LEASE_S == 7200
    assert cfg.lease_warn_s == store.lease_warn_s == LEASE_WARN_S == 900
    assert cfg.review_overdue_s == store.review_overdue_s == REVIEW_OVERDUE_S == 14400


def test_config_lease_warning_changes_alert_reader(w, configured_store):
    store = configured_store({})
    (key,) = seed(w, [("A", [])])
    store.claim(w.p("l1main"), key, lease_s=1200)
    assert store.alerts(w.p("reader"))["lease_expiring"] == []
    store = configured_store({"lease_warn_s": 1800})
    assert [item["key"] for item in store.alerts(w.p("reader"))["lease_expiring"]] == [key]


def test_config_review_overdue_changes_alert_reader(w, configured_store):
    store = configured_store({})
    (key,) = seed(w, [("A", [])])
    to_in_review(w, key)
    w.clock.advance(1201)
    assert store.alerts(w.p("reader"))["review_overdue"] == []
    store = configured_store({"review_overdue_s": 600})
    assert store.alerts(w.p("reader"))["review_overdue"] == [key]


def test_configured_heartbeat_still_never_shortens(w, configured_store):
    store = configured_store({"default_lease": 600})
    (key,) = seed(w, [("A", [])])
    claim = store.claim(w.p("l1main"), key, lease_s=7200)
    w.clock.advance(60)
    first = store.heartbeat(w.p("l1main"), key, claim["epoch"])
    assert first["lease_until"] == claim["lease_until"]
    w.clock.advance(6600)
    second = store.heartbeat(w.p("l1main"), key, claim["epoch"])
    assert second["lease_until"] == store._iso(w.clock() + 600)
    assert second["lease_until"] > first["lease_until"]
    assert store.verify_integrity() == []
