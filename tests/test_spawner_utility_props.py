"""RED-first property tests for plangraph/spawner/resolver.py.

Every test below fails against the current resolver.py and passes once the
resolver honours the stated contract. One test per pathology, with the
file:line of the pathology in resolver.py:

* test_nan_terminates
    pathology: plangraph/spawner/resolver.py:304-306 - aa_intelligence,
    cost_per_task_usd, pool_burn_weight and verified_tok_per_s are coerced with
    ``float(x or default)`` / a None default, so NaN, inf and missing numerics
    silently flow through scoring instead of raising ValueError.
* test_negative_cost_rejected
    pathology: plangraph/spawner/resolver.py:305 and 326 - cost_per_task_usd is
    consumed raw, so a negative cost *increases* utility instead of raising.
* test_floor_critical_high
    pathology: plangraph/spawner/resolver.py:638-640 - when no model reaches
    RISK_MIN_INTELLIGENCE[risk] the floor is silently relaxed: no ValueError,
    no relax_floor/relax_reason argument, no "relaxed" flag in the result.
* test_unknown_risk_raises
    pathology: plangraph/spawner/resolver.py:313-316 (also 474 and 620) -
    RISK_*.get(risk, default) silently normalizes an unknown/empty risk band.
* test_unknown_role_raises_and_drive_works
    pathology: plangraph/spawner/resolver.py:317 (ROLE_ALPHA.get(role, 1.0))
    plus the role tables at resolver.py:30-55, which define "driver" but not
    the "drive" alias.
* test_zero_intelligence_is_zero
    pathology: plangraph/spawner/resolver.py:304 - ``float(x or 40.0)`` turns a
    real 0.0 intelligence (and a missing one) into 40.0.
"""

from __future__ import annotations

import signal

import pytest

from plangraph.spawner.resolver import (
    ROLE_ALPHA,
    ROLE_TRAJECTORY_FACTORS,
    rank_candidates,
    resolve_model_for_task,
    resolve_review_plan,
    score_model,
)

# --------------------------------------------------------------------------- #
# hermetic helpers
# --------------------------------------------------------------------------- #

def call_with_timeout(fn, seconds=5):
    """Run fn() but raise TimeoutError instead of hanging forever."""

    def _on_alarm(signum, frame):
        raise TimeoutError(f"call exceeded {seconds}s")

    previous_handler = signal.signal(signal.SIGALRM, _on_alarm)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        return fn()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)

class FakeDB:
    """Hermetic stand-in for CatalogDB; always passed as db=FakeDB(rows)."""

    def __init__(self, rows):
        self._rows = [dict(r) for r in rows]

    def list_models(self, active_only=False):
        return [dict(r) for r in self._rows]

def row(
    id,
    intel,
    cost=0.2,
    tier="subscription",
    speed=100.0,
    ctx=1000000,
    combos=("l2-worker", "l2-reviewer"),
    **kw,
):
    family = kw.pop("family", id.split("/", 1)[0])
    burn = kw.pop("pool_burn_weight", 1.0)
    available = kw.pop("available", 1)
    data = {
        "id": id,
        "family": family,
        "aa_intelligence": intel,
        "verified_tok_per_s": speed,
        "cost_per_task_usd": cost,
        "pool_burn_weight": burn,
        "tier": tier,
        "available": available,
        "context_window": ctx,
        "combos": list(combos),
    }
    data.update(kw)
    return data

# --------------------------------------------------------------------------- #
# pathologies
# --------------------------------------------------------------------------- #

BAD_FIELD_VALUES = [
    ("aa_intelligence", float("nan")),
    ("aa_intelligence", float("inf")),
    ("aa_intelligence", float("-inf")),
    ("aa_intelligence", -1.0),
    ("aa_intelligence", None),
    ("cost_per_task_usd", float("nan")),
    ("cost_per_task_usd", float("inf")),
    ("cost_per_task_usd", float("-inf")),
    ("cost_per_task_usd", -0.01),
    ("cost_per_task_usd", None),
    ("pool_burn_weight", float("nan")),
    ("pool_burn_weight", float("inf")),
    ("pool_burn_weight", float("-inf")),
    ("pool_burn_weight", -0.5),
    ("pool_burn_weight", None),
    ("verified_tok_per_s", float("nan")),
    ("verified_tok_per_s", float("inf")),
    ("verified_tok_per_s", float("-inf")),
]

@pytest.mark.parametrize("field,bad", BAD_FIELD_VALUES)
def test_nan_terminates(field, bad):
    """NaN/inf/None numerics must raise ValueError, never score or hang."""
    model = row("fam-a/model-x", 46.0)
    model[field] = bad

    with pytest.raises(ValueError):
        call_with_timeout(
            lambda: score_model(model, role="implement", risk="normal")
        )
    with pytest.raises(ValueError):
        call_with_timeout(
            lambda: rank_candidates([model], role="implement", risk="normal")
        )

    # ...but a missing speed is explicitly allowed as "missing".
    speedless = row("fam-a/speedless-model", 46.0)
    speedless["verified_tok_per_s"] = None
    scored = call_with_timeout(
        lambda: score_model(speedless, role="implement", risk="normal")
    )
    assert isinstance(scored, float)

def test_negative_cost_rejected():
    """A negative cost must not raise utility; it must raise ValueError."""
    cheap = row("fam-a/negative-cost", 46.0, cost=-0.5)
    with pytest.raises(ValueError):
        call_with_timeout(
            lambda: score_model(cheap, role="implement", risk="normal")
        )

@pytest.mark.parametrize("risk", ["high", "critical"])
def test_floor_critical_high(risk):
    """Risk floor must fail closed, and relax only with an explicit reason."""
    floor = {"high": 46.0, "critical": 50.0}[risk]
    weak_rows = [
        row("fam-low/weak-one", floor - 8.0, cost=0.5),
        row("fam-low/weak-two", floor - 2.0, cost=0.5),
        row("fam-low/weak-three", floor - 20.0, cost=0.5),
    ]
    weak_db = FakeDB(weak_rows)

    # No model meets the floor and no relaxation was requested -> ValueError.
    with pytest.raises(ValueError):
        call_with_timeout(
            lambda: resolve_model_for_task(
                role="implement", risk=risk, db=weak_db
            )
        )

    # Relaxation without a reason is not allowed.
    with pytest.raises(ValueError):
        call_with_timeout(
            lambda: resolve_model_for_task(
                role="implement",
                risk=risk,
                db=weak_db,
                relax_floor=True,
                relax_reason="",
            )
        )

    # Relaxation with a reason is allowed and must be signalled.
    forced = call_with_timeout(
        lambda: resolve_model_for_task(
            role="implement",
            risk=risk,
            db=weak_db,
            relax_floor=True,
            relax_reason="needed",
        )
    )
    assert isinstance(forced, dict)
    assert forced["relaxed"] is True
    assert isinstance(forced["model"], str) and forced["model"]

    # When a qualifying model exists it is chosen and nothing is relaxed.
    strong_db = FakeDB(
        [
            row("fam-low/weak-one", floor - 8.0, cost=0.5),
            row("fam-top/strong-one", floor + 20.0, cost=0.2),
        ]
    )
    chosen = call_with_timeout(
        lambda: resolve_model_for_task(role="implement", risk=risk, db=strong_db)
    )
    assert chosen["relaxed"] is False
    assert chosen["model"] == "fam-top/strong-one"

@pytest.mark.parametrize("risk", ["bogus", ""])
def test_unknown_risk_raises(risk):
    """Unknown/empty risk bands must raise ValueError everywhere."""
    db = FakeDB(
        [
            row("fam-a/model-a", 46.0),
            row("fam-b/model-b", 48.0, family="fam-b"),
        ]
    )
    model = row("fam-a/model-a", 46.0)

    with pytest.raises(ValueError):
        call_with_timeout(
            lambda: score_model(model, role="implement", risk=risk)
        )
    with pytest.raises(ValueError):
        call_with_timeout(
            lambda: rank_candidates([model], role="implement", risk=risk)
        )
    with pytest.raises(ValueError):
        call_with_timeout(
            lambda: resolve_model_for_task(role="implement", risk=risk, db=db)
        )
    with pytest.raises(ValueError):
        call_with_timeout(
            lambda: resolve_review_plan(
                writer_model="fam-a/model-a", risk=risk, db=db
            )
        )

def test_unknown_role_raises_and_drive_works():
    """Unknown roles raise; "drive" is a real alias of "driver"."""
    assert "drive" in ROLE_ALPHA
    assert "driver" in ROLE_ALPHA
    assert "drive" in ROLE_TRAJECTORY_FACTORS
    assert "driver" in ROLE_TRAJECTORY_FACTORS
    assert ROLE_ALPHA["drive"] == ROLE_ALPHA["driver"]
    assert ROLE_TRAJECTORY_FACTORS["drive"] == ROLE_TRAJECTORY_FACTORS["driver"]

    db = FakeDB(
        [
            row("fam-a/fast-model", 46.0, speed=200.0, cost=0.2),
            row("fam-b/slow-model", 46.0, speed=10.0, cost=0.2, family="fam-b"),
        ]
    )

    with pytest.raises(ValueError):
        call_with_timeout(
            lambda: resolve_model_for_task(role="bogus", risk="normal", db=db)
        )

    via_drive = call_with_timeout(
        lambda: resolve_model_for_task(role="drive", risk="normal", db=db)
    )
    via_driver = call_with_timeout(
        lambda: resolve_model_for_task(role="driver", risk="normal", db=db)
    )

    assert via_drive["model"] == via_driver["model"]
    assert via_drive["family"] == via_driver["family"]
    assert via_drive["tier"] == via_driver["tier"]
    assert score_model(
        via_drive["candidate"], role="drive", risk="normal"
    ) == score_model(via_driver["candidate"], role="driver", risk="normal")

def test_zero_intelligence_is_zero():
    """0.0 intelligence scores strictly below 40.0; missing intel raises."""
    low = row("fam-a/model-zero", 0.0)
    high = row("fam-a/model-forty", 40.0)

    score_low = call_with_timeout(
        lambda: score_model(low, role="implement", risk="normal")
    )
    score_high = call_with_timeout(
        lambda: score_model(high, role="implement", risk="normal")
    )
    assert score_low < score_high

    absent = row("fam-a/model-absent", 40.0)
    del absent["aa_intelligence"]
    with pytest.raises(ValueError):
        call_with_timeout(
            lambda: score_model(absent, role="implement", risk="normal")
        )

    explicit_none = row("fam-a/model-none", 40.0)
    explicit_none["aa_intelligence"] = None
    with pytest.raises(ValueError):
        call_with_timeout(
            lambda: score_model(explicit_none, role="implement", risk="normal")
        )


@pytest.mark.parametrize(
    "bad_need_tokens",
    [0, -1, float("nan"), float("inf"), True, None, "100"],
)
def test_need_tokens_validated(bad_need_tokens):
    r = row("fam0/a", 50.0)
    r2 = row("fam1/b", 51.0)
    db = FakeDB([r, r2])

    calls = [
        lambda: score_model(r, need_tokens=bad_need_tokens),
        lambda: rank_candidates([r, r2], need_tokens=bad_need_tokens),
        lambda: resolve_model_for_task(db=db, need_tokens=bad_need_tokens),
        lambda: resolve_review_plan("mimo/x", db=db, need_tokens=bad_need_tokens),
    ]
    for call in calls:
        with pytest.raises(ValueError):
            call_with_timeout(call)

    call_with_timeout(lambda: resolve_model_for_task(db=db, need_tokens=32000))
    call_with_timeout(lambda: resolve_model_for_task(db=db, need_tokens=32000.0))
