"""Pins that run WITH console credit legs present (D-3258/D-3259/D-3264, slice G4).

Each pin runs under an unexpired credit policy and a host listing stubbed to
contain the console-anthropic models, so the credit legs are actually routed:
under the neutral conftest policy they are skipped as ``credit_expired``, and
without positive host evidence they are ``not_served_on_host`` on Linux.
"""
from datetime import date, timedelta

from plangraph.planner.legs import DEFAULT_LEGS, WRITER_ORDER
from plangraph.planner.route_policy import RoutePolicy
from plangraph.planner.routing import next_leg_after_provider_stop, plan_spawn
from plangraph.planner.seats import seat_family
from plangraph.routing_support.resolver import plan_for_launch
from plangraph.spawner.leg_state import LegStateStore

CARD = {"estimate_tokens": 1000}
CONSOLE_SONNET = "console-sonnet-5.5"
CONSOLE_SONNET_MODEL = "console-anthropic/claude-sonnet-5-5"
#: Host listing stubbed to contain the console-anthropic models (positive host evidence).
HOST_LISTING = frozenset(leg.model for leg in DEFAULT_LEGS)
POLICY = RoutePolicy(credit_tiers={"console-anthropic": {"expires": "2026-11-30"}},
                     today=date(2026, 11, 1))


def _ok(model, **kwargs):
    return {"state": "ok", "evidence": {}}


def _limited_console_sonnet(model, **kwargs):
    if model == CONSOLE_SONNET_MODEL:
        return {"state": "limited", "evidence": {"status": 429}}
    return _ok(model, **kwargs)


def _host_listing(env):
    return HOST_LISTING


def _writer_entry_after(leg_id, steps=1):
    """``steps`` WRITER_ORDER entries after ``leg_id``, computed (never a literal id)."""
    return WRITER_ORDER[WRITER_ORDER.index(leg_id) + steps]


def test_limited_console_leg_falls_through_to_next_writer_entry():
    """(a) A limited console-sonnet-5.5 yields the next WRITER_ORDER entry first."""
    plan = plan_spawn(CARD, role="implement", leg_reader=_limited_console_sonnet, policy=POLICY)
    assert plan.first_leg.id == _writer_entry_after(CONSOLE_SONNET)
    assert CONSOLE_SONNET not in {leg.id for leg in plan.legs}
    assert any(skip["leg"] == CONSOLE_SONNET and skip["reason"] == "limited"
               for skip in plan.skipped)


def test_provider_stop_on_console_leg_returns_next_non_limited_leg(tmp_path):
    """(b) A provider stop on console-sonnet-5.5 advances to the next non-limited leg."""
    store = LegStateStore(tmp_path / "state.sqlite")
    plan = plan_spawn(CARD, role="implement", leg_reader=store, policy=POLICY, now=100)
    launched = plan_for_launch(plan, probe=_host_listing)
    assert launched.first_leg.id == CONSOLE_SONNET
    # The entry right after console-sonnet-5.5 is limited too (its own 429):
    # the walk must skip it and return the next non-limited leg.
    assert plan.legs[1].id == _writer_entry_after(CONSOLE_SONNET)
    store.record_provider_response(plan.legs[1].model, 429, "quota exhausted",
                                   {"Retry-After": "60"}, "test", now=100)
    nxt = next_leg_after_provider_stop(launched, CONSOLE_SONNET, 429, "quota exhausted",
                                       {"Retry-After": "60"}, "test", store=store, now=100)
    assert nxt.id == _writer_entry_after(CONSOLE_SONNET, steps=2)
    assert nxt.id != plan.legs[1].id


def test_review_seats_for_gpt_writer_include_console_leg_without_family_doubles():
    """(c) A gpt writer's seats include a console leg and never two legs of one family."""
    plan = plan_spawn(CARD, role="review", writer_families=("gpt",), leg_reader=_ok, policy=POLICY)
    launched = plan_for_launch(plan, probe=_host_listing)
    seats = launched.review_seats
    console_seat = next((leg for leg in seats if leg.id.startswith("console-")), None)
    assert console_seat is not None and console_seat in launched.legs
    assert len(seats) >= 2
    families = [seat_family(leg.family) for leg in seats]
    assert len(families) == len(set(families))


# ---------------------------------------------------------------------------
# D-3258/D-3259/D-3264 (r3): spend capture, ledger lock, credit report and
# skip-record pins. Every one of these fails on the pre-fix tree.
# ---------------------------------------------------------------------------
import json
import os
from types import SimpleNamespace

from plangraph.routing_support import credit_gate
from plangraph.spawner import credit

CREDIT_LEG = next(leg for leg in DEFAULT_LEGS if leg.tier == "credit")
NON_CREDIT_TIERS = ("trial", "free", "subscription", "paid", "")


def _job(job_id="j"):
    return SimpleNamespace(job_id=job_id, start_time=None, status="failed")


def test_capture_spend_reads_the_store_only_for_credit_tier_legs(monkeypatch):
    """Capture belongs to the chosen leg's tier: non-credit legs never touch the store."""
    calls = []
    monkeypatch.setattr(credit, "session_spend",
                        lambda *a, **k: calls.append(a) or credit.unavailable("stub"))
    job = _job()
    for tier in NON_CREDIT_TIERS:
        credit.capture_spend(job, {"XDG_DATA_HOME": "/nowhere"}, tier=tier)
    assert calls == [], "spend capture must not run for non-credit legs"
    credit.capture_spend(job, {"XDG_DATA_HOME": "/nowhere"}, tier="credit")
    assert len(calls) == 1 and job.credit_spend["source"] == "unavailable"


def test_capture_spend_is_best_effort_and_never_raises(monkeypatch):
    """Spend capture can never fail a run: no exception escapes it, even on garbage env."""
    monkeypatch.setattr(credit, "session_spend",
                        lambda *a, **k: credit.unavailable("stub"))
    for env in ({"XDG_DATA_HOME": "\0bogus"}, {"XDG_DATA_HOME": "/dev/null/nope"}, {}):
        credit.capture_spend(_job(), env, tier="credit")  # must not raise
    credit.capture_spend(None, {}, tier="credit")  # dropped job: log and return


def test_ledger_lock_is_distinct_from_the_ledger_and_reclaims_stale_locks(tmp_path):
    """A crashed writer's stale lock is reclaimed; the lock never lives in the
    ledger basename family (``credit-spend.lock`` collides with the ledger's
    own suffix handling)."""
    path = tmp_path / "credit-spend.jsonl"
    lock = credit.ledger_lock_path(path)
    assert lock != path.with_suffix(".lock"), "lock name collides with the ledger basename family"
    assert lock != path
    lock.mkdir()
    stale = os.path.getmtime(lock) - 3600
    os.utime(lock, (stale, stale))  # a crashed writer left it behind
    with credit.ledger_lock(path):
        pass  # reclaimed without a busy-wait spin


def test_record_spend_reports_a_busy_ledger_instead_of_dropping_spend(tmp_path, monkeypatch):
    """A live lock never silently drops spend: the unavailability lands in the record."""
    monkeypatch.setattr(credit.time, "sleep", lambda _: None)
    credit.ledger_lock_path(tmp_path / "credit-spend.jsonl").mkdir()  # a live writer holds it
    job = SimpleNamespace(job_id="j", status="failed",
                          credit_spend={"cost_usd": 2.0, "tokens_input": 3,
                                        "tokens_output": 4, "source": "opencode"})
    record = {"run_id": "r"}
    credit.record_spend(tmp_path, record, CREDIT_LEG, job)
    assert record["credit_spend"]["source"] == "unavailable"
    assert record["credit_spend"]["reason"], "the dropped spend must say why"


def test_torn_ledger_lines_are_preserved_verbatim(tmp_path):
    """A torn line keeps its original bytes: rewrite never replaces evidence."""
    path = tmp_path / "credit-spend.jsonl"
    torn = b'{"run_id": "torn-\xff\xfe'
    path.write_bytes(torn + b"\n")
    job = SimpleNamespace(job_id="j", status="completed",
                          credit_spend={"cost_usd": 1.0, "tokens_input": 1,
                                        "tokens_output": 1, "source": "opencode"})
    credit.record_spend(tmp_path, {"run_id": "r"}, CREDIT_LEG, job)
    assert path.read_bytes().startswith(torn), "the torn line's bytes were destroyed"
    row = credit.credit_report(tmp_path, policy=POLICY)[0]
    assert row["malformed_lines"] == 1


def test_credit_report_never_raises_on_missing_or_unparsable_expiry(tmp_path):
    """{} or "expires": "nov" gates as credit_expired: no KeyError/ValueError."""
    for entry in ({}, {"expires": "nov"}):
        policy = RoutePolicy(credit_tiers={"console-anthropic": dict(entry)})
        row = credit.credit_report(tmp_path, policy=policy)[0]
        assert row["days_left"] is None
        assert row["reason"] == "credit_expired"


def test_credit_table_lists_the_columns_the_docs_advertise():
    """SPAWNER.md promises unknown/unavailable/malformed counts; the table prints them."""
    for column in ("unknown_runs", "unavailable_runs", "malformed_lines"):
        assert column in credit.TABLE_COLUMNS, column
        assert column in credit.credit_table([dict.fromkeys(credit.TABLE_COLUMNS, 0)])


def test_credit_gate_re_evaluates_today_on_every_call(tmp_path, monkeypatch):
    """A cached policy must not freeze 'today': a long-lived process re-gates
    per call as the day rolls over."""
    import plangraph.planner.route_policy as route_policy
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(json.dumps({"credit_tiers": {"console-anthropic": {"expires": tomorrow}}}))
    monkeypatch.setenv("PLANGRAPH_ROUTE_POLICY", str(policy_file))
    credit_gate.clear_policy_cache()
    row = {"id": "console-anthropic/x", "tier": "credit"}
    assert credit_gate.catalog_credit_skip_reason(row) is None

    real_datetime = route_policy.datetime

    class _DateTime:
        @staticmethod
        def now(tz=None):
            return real_datetime.now(tz) + timedelta(days=3)

    monkeypatch.setattr(route_policy, "datetime", _DateTime)
    assert credit_gate.catalog_credit_skip_reason(row) == "credit_expired"  # no clear_policy_cache


def test_plan_skip_records_carry_skip_reason_for_w30_consumers():
    """W30 consumers read ``skip_reason``; every plan skip record spells it."""
    plan = plan_spawn(CARD, role="implement", leg_reader=_ok,
                      # The production ban pair (kimi, deepseek); DEFAULT_LEGS
                      # carries deepseek legs (kimi has none), so the ban yields
                      # the skip records this pin reads.
                      policy=RoutePolicy(banned_families=("kimi", "deepseek"),
                                         credit_tiers={"console-anthropic": {"expires": "2026-11-30"}}))
    skipped = plan.to_dict()["skipped"]
    assert skipped, "the banned family must produce skip records"
    for entry in skipped:
        assert entry.get("skip_reason") == entry.get("reason"), entry


def test_claude_family_writer_gets_no_claude_seat():
    """(d) A claude-family writer's review seats never include a claude leg."""
    plan = plan_spawn(CARD, role="review", writer_families=("claude",), leg_reader=_ok, policy=POLICY)
    seats = plan.review_seats
    assert len(seats) >= 2
    assert all(seat_family(leg.family) != seat_family("claude") for leg in seats)
