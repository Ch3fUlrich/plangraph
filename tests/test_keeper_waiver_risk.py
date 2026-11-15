"""Auth/secrets card risk cannot be waived down to one non-Anthropic seat."""
import pytest

from evidence_fakes import FakeDecisions, FakeGit, make_ctx
from keeper_util import to_in_review
from plangraph.keeper import auth
from plangraph.keeper.errors import Refused
from test_keeper_waiver import DECISION, KEY, _count, w


def _reviewer(w, name, family):
    token = auth.add_principal(w.conn, name, "reviewer", ["demo"], family=family)
    return auth.authenticate(w.conn, token)


@pytest.mark.parametrize("risk", ["auth", "secrets"])
@pytest.mark.parametrize("kind", ["decision", "commit+ci"])
@pytest.mark.parametrize("waiver_first", [False, True])
@pytest.mark.parametrize("bad_family", ["anthropic", "claude-sonnet", "google", "gemini-test"])
def test_waiver_auth_secrets_need_two_non_anthropic_families(
        w, risk, kind, waiver_first, bad_family):
    sha = "a" * 40
    w.store.ctx = make_ctx(
        git=FakeGit(trailers_by_sha={sha: {"Writer": "mimo-v2 run1"}}),
        decisions=FakeDecisions(waiving={(DECISION, KEY)}))
    w.store.put_card(w.p("router"), KEY, {"outcome": "fixture", "risk_tags": [risk]})
    to_in_review(w, KEY, kind=kind,
                 ref={"commit": sha} if kind == "commit+ci" else {"id": "D-1"})
    first = _reviewer(w, "first", "google")
    if waiver_first:
        w.store.waive_second_family(w.p("router"), KEY, DECISION)
    assert w.store.verdict(first, KEY, True) == {
        "key": KEY, "state": "in_review", "reviews_passed": 1}
    if not waiver_first:
        w.store.waive_second_family(w.p("router"), KEY, DECISION)
    bad = _reviewer(w, "bad", bad_family)
    before = w.store._item(KEY)
    with pytest.raises(Refused) as exc:
        w.store.verdict(bad, KEY, True)
    assert exc.value.code == "coverage"
    assert "insufficient_non_anthropic_seat_families: need 2" in exc.value.msg
    assert w.store._item(KEY) == before
    assert _count(w, KEY, "review_verdict") == 1
    assert _count(w, KEY, "review_passed") == 0
    second = _reviewer(w, "second", "deepseek")
    assert w.store.verdict(second, KEY, True) == {
        "key": KEY, "state": "done", "reviews_passed": 2}
    assert _count(w, KEY, "review_verdict") == 2
    assert _count(w, KEY, "review_passed") == 1
    assert w.store.verify_chain() == []
    assert w.store.verify_integrity() == []


@pytest.mark.parametrize("raw", ["null", "{}", '"auth"', "[", '["Auth"]',
                                 '[" secrets "]', '["unknown"]', '[null]'])
def test_waiver_invalid_card_risk_tags_fail_closed(w, raw):
    w.store.put_card(w.p("router"), KEY, {"outcome": "fixture", "risk_tags": []})
    to_in_review(w, KEY)
    w.store.waive_second_family(w.p("router"), KEY, DECISION)
    w.conn.execute("UPDATE cards SET risk_tags=? WHERE key=?", (raw, KEY))
    before = w.store._item(KEY)
    with pytest.raises(Refused) as exc:
        w.store.verdict(w.p("reviewer"), KEY, True)
    assert exc.value.code == "validation"
    assert exc.value.msg == "card_risk_tags_invalid"
    assert w.store._item(KEY) == before
    assert _count(w, KEY, "review_verdict") == 0
    assert _count(w, KEY, "review_passed") == 0


@pytest.mark.parametrize("tags", [[], ["money"], ["migration"], ["public-contract"], ["live-step"]])
def test_waiver_non_auth_card_still_closes_after_one_verdict(w, tags):
    w.store.put_card(w.p("router"), KEY, {"outcome": "fixture", "risk_tags": tags})
    to_in_review(w, KEY)
    w.store.waive_second_family(w.p("router"), KEY, DECISION)
    assert w.store.verdict(w.p("reviewer"), KEY, True) == {
        "key": KEY, "state": "done", "reviews_passed": 1}
    assert w.store.verify_integrity() == []


@pytest.mark.parametrize("raw,state", [('["auth"]', "in_review"),
                                     ('["secrets"]', "in_review"), ("null", None)])
def test_waiver_reads_card_risk_inside_verdict_write_lock(w, monkeypatch, raw, state):
    w.store.put_card(w.p("router"), KEY, {"outcome": "fixture", "risk_tags": []})
    to_in_review(w, KEY)
    w.store.waive_second_family(w.p("router"), KEY, DECISION)
    entered = []

    class LockedConnection:
        def __getattr__(self, name):
            return getattr(w.conn, name)

        def execute(self, sql, *args, **kwargs):
            cursor = w.conn.execute(sql, *args, **kwargs)
            if sql == "BEGIN IMMEDIATE":
                assert w.conn.in_transaction
                entered.append(sql)
                w.conn.execute("UPDATE cards SET risk_tags=? WHERE key=?", (raw, KEY))
            return cursor

    monkeypatch.setattr(w.store, "conn", LockedConnection())
    first = _reviewer(w, "first", "google")
    if state:
        assert w.store.verdict(first, KEY, True) == {
            "key": KEY, "state": state, "reviews_passed": 1}
        assert _count(w, KEY, "review_verdict") == 1
    else:
        with pytest.raises(Refused) as exc:
            w.store.verdict(first, KEY, True)
        assert exc.value.code == "validation"
        assert exc.value.msg == "card_risk_tags_invalid"
        assert _count(w, KEY, "review_verdict") == 0
    assert entered == ["BEGIN IMMEDIATE"]
    assert w.store._item(KEY)["state"] == "in_review"
    assert _count(w, KEY, "review_passed") == 0
    assert w.store.verify_integrity() == []
