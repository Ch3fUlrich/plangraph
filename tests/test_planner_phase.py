"""planner.phase: plan a gate's cards for the Phase runner (S1).

Portable like test_planner_service: ``World`` follows ``PG_KEEPER_BACKEND``,
repo trees live under ``tmp_path`` and nothing is executed for real.
"""
import pytest

from door_util import Doors
from keeper_util import World, defn
from plangraph.governor import policy
from plangraph.keeper.api import KeeperHandler
from plangraph.keeper import api_routes
from plangraph.keeper.errors import Refused
from plangraph.planner import phase, shadow
from plangraph.planner.legs import DEFAULT_LEGS

@pytest.fixture
def w(tmp_path):
    world = World(tmp_path / "keeper")
    yield world
    world.close()

@pytest.fixture
def doors(w, tmp_path):
    live = Doors(w, tmp_path)
    try:
        yield live
    finally:
        live.close()

@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("A = 1\n", encoding="utf-8")
    (root / "src" / "b.py").write_text("B = 2\n", encoding="utf-8")
    return root

def card(**kw):
    base = {"outcome": "add the thing", "write_set": ["src/a.py"],
            "read_set": ["src/b.py"], "check_cmd": "true",
            "window_class": "128k", "risk_tags": [], "estimate_tokens": 40000}
    base.update(kw)
    return base

def make_phase(w):
    w.store.create_items(w.p("router"), [defn("C1"), defn("C2"),
        defn("PH-X", ["C1", "C2"], kind="gate"),
        defn("GOAL", ["PH-X"], kind="goal")])

def code_of(exc):
    return getattr(exc, "code", None) or exc.args[0]

PLAN_KEYS = {"review_method", "review_scope", "phase", "ready", "blocked", "cards",
             "overlaps", "skipped", "busy"}
CARD_KEYS = {"key", "state", "write_set", "ready", "findings",
             "start_allowed", "start_reason", "route",
             "route_error", "shadow_id", "deferred"}
ROUTE_KEYS = {"window_class", "harness", "legs", "review_seats"}

# --------------------------------------------------------------------------- #
# planning
# --------------------------------------------------------------------------- #

def test_two_ready_cards_disjoint(w, repo):
    make_phase(w)
    w.store.put_card(w.p("router"), "demo:C1", card(write_set=["src/a.py"]))
    w.store.put_card(w.p("router"), "demo:C2", card(write_set=["src/b.py"]))

    plan = phase.plan_phase(w.store, w.p("router"), "demo:PH-X", repo_root=repo)

    assert set(plan) == PLAN_KEYS
    assert plan["phase"] == "demo:PH-X"
    assert plan["review_method"]["mode"] == "batch"
    assert plan["ready"] is True
    assert plan["blocked"] == []
    assert plan["skipped"] == [] and plan["busy"] == [] and plan["overlaps"] == []
    assert phase.startable(plan) == ["demo:C1", "demo:C2"]

    non_opencode = {leg.id for leg in DEFAULT_LEGS
                    if leg.harness in ("qodercli", "agy")}
    for c in plan["cards"]:
        assert set(c) == CARD_KEYS
        assert c["ready"] is True and c["deferred"] is False
        assert c["start_allowed"] is True
        assert c["state"] == "open"
        assert c["route_error"] is None
        assert set(c["route"]) == ROUTE_KEYS
        assert c["route"]["harness"] == "opencode"
        assert c["route"]["window_class"] == "128k"
        assert not (set(c["route"]["legs"]) & non_opencode)

def test_needs_input_and_ready_cards_are_planned(w, repo):
    make_phase(w)
    w.store.put_card(w.p("router"), "demo:C1", card(write_set=["src/a.py"]))
    w.store.put_card(w.p("router"), "demo:C2", card(write_set=["src/b.py"]))
    w.store.set_state(w.p("router"), "demo:C1", "needs_input")
    w.store.set_state(w.p("router"), "demo:C2", "ready")

    plan = phase.plan_phase(w.store, w.p("router"), "demo:PH-X", repo_root=repo)

    by_key = {c["key"]: c for c in plan["cards"]}
    assert set(by_key) == {"demo:C1", "demo:C2"}
    assert by_key["demo:C1"]["state"] == "needs_input"
    assert by_key["demo:C2"]["state"] == "ready"
    assert plan["skipped"] == [] and plan["busy"] == []

def test_overlapping_write_sets_defer_second(w, repo):
    make_phase(w)
    w.store.put_card(w.p("router"), "demo:C1", card(write_set=["src/a.py"]))
    w.store.put_card(w.p("router"), "demo:C2", card(write_set=["src/a.py"]))

    plan = phase.plan_phase(w.store, w.p("router"), "demo:PH-X", repo_root=repo)

    assert plan["overlaps"] == [
        {"a": "demo:C1", "b": "demo:C2",
         "path_a": "src/a.py", "path_b": "src/a.py"}]
    assert plan["cards"][0]["deferred"] is False
    assert plan["cards"][1]["deferred"] is True
    assert phase.startable(plan) == ["demo:C1"]
    assert plan["ready"] is True

def test_failing_card_not_ready_phase_still_ready(w, repo):
    make_phase(w)
    w.store.put_card(w.p("router"), "demo:C1",
                     card(write_set=["src/a.py"], check_cmd=""))
    w.store.put_card(w.p("router"), "demo:C2", card(write_set=["src/b.py"]))

    plan = phase.plan_phase(w.store, w.p("router"), "demo:PH-X", repo_root=repo)

    by_key = {c["key"]: c for c in plan["cards"]}
    assert by_key["demo:C1"]["ready"] is False
    assert "no_check" in [f["code"] for f in by_key["demo:C1"]["findings"]]
    assert by_key["demo:C2"]["ready"] is True
    assert phase.startable(plan) == ["demo:C2"]
    assert plan["ready"] is True

def test_done_and_busy_dsts_not_planned(w, repo):
    make_phase(w)
    w.store.put_card(w.p("router"), "demo:C1", card())
    w.store.put_card(w.p("router"), "demo:C2", card())
    w.conn.execute("UPDATE items SET state = ? WHERE key = ?",
                   ("done", "demo:C1"))
    w.conn.execute("UPDATE items SET state = ? WHERE key = ?",
                   ("running", "demo:C2"))
    w.conn.commit()

    plan = phase.plan_phase(w.store, w.p("router"), "demo:PH-X", repo_root=repo)

    assert plan["skipped"] == [{"key": "demo:C1", "why": "already_done"}]
    assert plan["busy"] == [{"key": "demo:C2", "state": "running"}]
    assert plan["cards"] == []
    assert plan["ready"] is False

def test_unclassified_and_done_dsts_are_reported_once(w, repo):
    w.store.create_items(w.p("router"), [
        defn("C1"), defn("C2"), defn("C3"), defn("C4"), defn("C5"),
        defn("PH-X", ["C1", "C2", "C3", "C4", "C5"], kind="gate"),
        defn("GOAL", ["PH-X"], kind="goal"),
    ])
    w.store.put_card(w.p("router"), "demo:C1", card(write_set=["src/a.py"]))
    w.store.set_state(w.p("router"), "demo:C2", "running")
    w.conn.execute("UPDATE items SET state = ? WHERE key = ?",
                   ("done", "demo:C3"))
    w.conn.execute("UPDATE items SET state = ? WHERE key = ?",
                   ("blocked", "demo:C5"))
    w.conn.commit()

    plan = phase.plan_phase(w.store, w.p("router"), "demo:PH-X", repo_root=repo)

    assert {c["key"] for c in plan["cards"]} == {"demo:C1"}
    assert {b["key"]: b["state"] for b in plan["busy"]} == {
        "demo:C2": "running", "demo:C5": "blocked"}
    assert {"key": "demo:C3", "why": "already_done"} in plan["skipped"]
    assert {"key": "demo:C4", "why": "missing_card"} in plan["skipped"]

    dsts = {r[0] for r in w.conn.execute(
        "SELECT dst FROM edges WHERE src = ?", ("demo:PH-X",)).fetchall()}
    seen = ([c["key"] for c in plan["cards"]]
            + [s["key"] for s in plan["skipped"]]
            + [b["key"] for b in plan["busy"]])
    assert len(seen) == len(set(seen))
    assert set(seen) == dsts

def test_guards(w, repo):
    w.store.create_items(w.p("router"), [defn("C1"), defn("L1"),
        defn("PH-X", ["C1"], kind="gate"),
        defn("GOAL", ["PH-X", "L1"], kind="goal")])
    w.store.put_card(w.p("router"), "demo:C1", card())

    with pytest.raises(Refused) as exc:
        phase.plan_phase(w.store, w.p("router"), "demo:L1", repo_root=repo)
    assert code_of(exc.value) == "validation"

    with pytest.raises(Refused) as exc:
        phase.plan_phase(w.store, w.p("router"), "demo:NOPE", repo_root=repo)
    assert code_of(exc.value) == "not_found"

    # role check runs first: a worker gets forbidden even for an unknown key
    with pytest.raises(Refused) as exc:
        phase.plan_phase(w.store, w.p("workerA"), "demo:NOPE", repo_root=repo)
    assert code_of(exc.value) == "forbidden"

    plan = phase.plan_phase(w.store, w.p("system"), "demo:PH-X", repo_root=repo)
    assert plan["phase"] == "demo:PH-X"
    assert plan["review_method"]["mode"] == "batch"

def test_reader_and_l1_forbidden(w, repo):
    make_phase(w)
    for who in ("reader", "l1main"):
        with pytest.raises(Refused) as exc:
            phase.plan_phase(w.store, w.p(who), "demo:PH-X", repo_root=repo)
        assert code_of(exc.value) == "forbidden"

def test_governor_denies_rate_limited_card(w, repo):
    make_phase(w)
    w.store.put_card(w.p("router"), "demo:C1", card(write_set=["src/a.py"]))

    base = phase.plan_phase(w.store, w.p("router"), "demo:PH-X", repo_root=repo)
    assert base["ready"] is True
    first = base["cards"][0]
    first_leg_id = first["route"]["legs"][0]
    family = {leg.id: leg for leg in DEFAULT_LEGS}[first_leg_id].family

    plan = phase.plan_phase(
        w.store, w.p("router"), "demo:PH-X", repo_root=repo,
        source=policy.StaticSource(rate_limited=frozenset({family})))

    got = next(c for c in plan["cards"] if c["key"] == first["key"])
    assert got["start_allowed"] is False
    assert got["start_reason"].startswith("provider_rate_limited")
    assert got["key"] not in phase.startable(plan)
    assert plan["ready"] is False

def test_no_opencode_leg(w, repo):
    make_phase(w)
    w.store.put_card(w.p("router"), "demo:C1", card())
    w.store.put_card(w.p("router"), "demo:C2", card(write_set=["src/b.py"]))

    plan = phase.plan_phase(w.store, w.p("router"), "demo:PH-X",
                            repo_root=repo, legs=())
    assert plan["ready"] is False
    assert "no opencode leg" in plan["blocked"]
    assert phase.startable(plan) == []

def test_writes_nothing(w, repo):
    make_phase(w)
    w.store.put_card(w.p("router"), "demo:C1", card())
    w.store.put_card(w.p("router"), "demo:C2", card(write_set=["src/b.py"]))

    def counts():
        return {table: w.conn.execute(
            f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("items", "cards", "edges", "events")}

    before = counts()
    phase.plan_phase(w.store, w.p("router"), "demo:PH-X", repo_root=repo)
    assert counts() == before

def test_shadow_hook_off_writes_no_file(w, repo, tmp_path, monkeypatch):
    make_phase(w)
    w.store.put_card(w.p("router"), "demo:C1", card(write_set=["src/a.py"]))
    w.store.put_card(w.p("router"), "demo:C2", card(write_set=["src/b.py"]))
    monkeypatch.delenv(shadow.ENV_FLAG, raising=False)
    monkeypatch.delenv(shadow.ENV_LOG, raising=False)

    def files():
        return {p for p in tmp_path.rglob("*") if p.is_file()}

    before = files()
    plan = phase.plan_phase(
        w.store, w.p("router"), "demo:PH-X", repo_root=repo,
        answerer=lambda _card, _route: {"legs": ["opencode-default"]})
    after = files()

    assert {c["key"] for c in plan["cards"]} == {"demo:C1", "demo:C2"}
    assert all(c["shadow_id"] is None for c in plan["cards"])
    assert after == before

def test_jev_routing_rows_land_in_the_keeper(w, repo, monkeypatch):
    """The routing production call site records through the real store.

    Fails if ``plan_phase`` stops handing ``store``/``principal`` to
    ``shadow.route_with_shadow`` (or drops that call): with no sink the Jev rows
    are built and thrown away, so the keeper stays empty.  The stub leg is the
    fake Jev -- deterministic, offline, no network.
    """
    make_phase(w)
    w.store.put_card(w.p("router"), "demo:C1", card(write_set=["src/a.py"]))
    w.store.put_card(w.p("router"), "demo:C2", card(write_set=["src/b.py"]))
    monkeypatch.setenv("PLANGRAPH_JEV", "stub")

    plan = phase.plan_phase(w.store, w.p("router"), "demo:PH-X", repo_root=repo)
    from plangraph.planner.jev_shadow import drain
    drain()

    rows = w.store.jev_rows("demo:C1")
    assert [r["kind"] for r in rows] == ["harness", "window", "budget", "review"]
    assert all(r["status"] == "ok" for r in rows)
    assert all(r["attempt"] == 1 for r in rows)
    assert all(r["jev_model"] == "stub" for r in rows)
    assert len({r["decision_id"] for r in rows}) == 1        # one decision per card
    assert len({r["input_hash"] for r in rows}) == 1

    rows2 = w.store.jev_rows("demo:C2")
    assert [r["kind"] for r in rows2] == ["harness", "window", "budget", "review"]
    assert {r["decision_id"] for r in rows} != {r["decision_id"] for r in rows2}

    # Jev changed nothing: the plan still carries the rules' route, no legacy id
    c1, = [c for c in plan["cards"] if c["key"] == "demo:C1"]
    assert c1["route"]["harness"] == "opencode"
    assert c1["route"]["window_class"] == "128k"
    assert c1["shadow_id"] is None

    # the rows are on show for that card
    shown = w.store.show(w.p("router"), "demo:C1")
    assert [r["kind"] for r in shown["jev_shadow"]] == ["harness", "window",
                                                        "budget", "review"]


def test_terminal_gate_is_bad_state(w, repo):
    make_phase(w)
    w.store.cancel(w.p("router"), "demo:PH-X", "test")

    with pytest.raises(Refused) as exc:
        phase.plan_phase(w.store, w.p("router"), "demo:PH-X", repo_root=repo)
    assert code_of(exc.value) == "bad_state"

# --------------------------------------------------------------------------- #
# HTTP handler
# --------------------------------------------------------------------------- #

def test_dispatcher_phase_plan_role_env_and_validation(doors, w, repo, monkeypatch):
    make_phase(w)
    w.store.put_card(w.p("router"), "demo:C1", card(write_set=["src/a.py"]))
    w.store.put_card(w.p("router"), "demo:C2", card(write_set=["src/b.py"]))
    monkeypatch.setenv("PG_PHASE_REPO_ROOT", str(repo))

    status, body = doors.http("router", "POST", "/v1/phase_plan",
                              {"key": "demo:PH-X"})
    assert status == 200
    assert body["phase"] == "demo:PH-X"
    assert phase.startable(body) == ["demo:C1", "demo:C2"]

    status, body = doors.http("workerA", "POST", "/v1/phase_plan",
                              {"key": "demo:PH-X"})
    assert (status, body["error"]) == (403, "forbidden")

    monkeypatch.delenv("PG_PHASE_REPO_ROOT", raising=False)
    status, body = doors.http("router", "POST", "/v1/phase_plan",
                              {"key": "demo:PH-X"})
    assert (status, body["error"]) == (503, "unavailable")

    status, body = doors.http("workerA", "POST", "/v1/phase_plan",
                              {"key": "demo:PH-X"})
    assert (status, body["error"]) == (403, "forbidden")

    status, body = doors.http("workerA", "POST", "/v1/phase_plan", {})
    assert (status, body["error"]) == (403, "forbidden")

    monkeypatch.setenv("PG_PHASE_REPO_ROOT", str(repo))
    status, body = doors.http("router", "POST", "/v1/phase_plan", {})
    assert (status, body["error"]) == (400, "validation")

def test_body_repo_root_is_ignored(w, repo, monkeypatch):
    make_phase(w)
    w.store.put_card(w.p("router"), "demo:C1", card(write_set=["src/a.py"]))
    w.store.put_card(w.p("router"), "demo:C2", card(write_set=["src/b.py"]))
    monkeypatch.setenv("PG_PHASE_REPO_ROOT", str(repo))

    body = {"key": "demo:PH-X", "repo_root": "/nonexistent"}
    result = api_routes._phase_plan(
        w.store, w.p("router"), body, KeeperHandler._need)
    direct = phase.plan_phase(w.store, w.p("router"), "demo:PH-X",
                              repo_root=repo)
    assert result == direct
    assert result["phase"] == "demo:PH-X"
    assert phase.startable(result) == ["demo:C1", "demo:C2"]

def test_handler_returns_plan(w, repo, monkeypatch):
    make_phase(w)
    w.store.put_card(w.p("router"), "demo:C1", card(write_set=["src/a.py"]))
    w.store.put_card(w.p("router"), "demo:C2", card(write_set=["src/b.py"]))
    monkeypatch.setenv("PG_PHASE_REPO_ROOT", str(repo))

    need = KeeperHandler._need
    result = api_routes._phase_plan(
        w.store, w.p("router"), {"key": "demo:PH-X"}, need)
    direct = phase.plan_phase(w.store, w.p("router"), "demo:PH-X",
                              repo_root=repo)
    assert result == direct
    assert result["phase"] == "demo:PH-X"

def test_handler_requires_env(w, repo, monkeypatch):
    make_phase(w)
    monkeypatch.delenv("PG_PHASE_REPO_ROOT", raising=False)
    with pytest.raises(Refused) as exc:
        api_routes._phase_plan(
            w.store, w.p("router"), {"key": "demo:PH-X"}, KeeperHandler._need)
    assert code_of(exc.value) == "unavailable"

def test_env_not_a_directory_is_unavailable(w, tmp_path, monkeypatch):
    make_phase(w)
    missing = tmp_path / "missing-repo"
    assert not missing.exists()
    monkeypatch.setenv("PG_PHASE_REPO_ROOT", str(missing))

    with pytest.raises(Refused) as exc:
        api_routes._phase_plan(
            w.store, w.p("router"), {"key": "demo:PH-X"}, KeeperHandler._need)
    assert code_of(exc.value) == "unavailable"

def test_handler_requires_key(w, repo, monkeypatch):
    make_phase(w)
    monkeypatch.setenv("PG_PHASE_REPO_ROOT", str(repo))
    with pytest.raises(Refused) as exc:
        api_routes._phase_plan(w.store, w.p("router"), {}, KeeperHandler._need)
    assert code_of(exc.value) == "validation"
