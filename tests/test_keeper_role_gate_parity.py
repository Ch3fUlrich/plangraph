"""ROLE_GATE (the seam's audited first check) equals each private's own check.

The private methods keep their own role checks for callers outside the seam
(``apply_ops`` batch dispatch, direct calls).  Each pair is compared by calling
the private with every role -- never by parsing source -- so the test fails if
either the table entry or the private's own list changes alone.
"""
import pytest

from keeper_util import World
from plangraph.keeper import auth
from plangraph.keeper.errors import Refused
from plangraph.keeper.store import ROLE_GATE
from plangraph.planner.phase import plan_phase
from plangraph.planner.phase_finish import finish_phase
from plangraph.planner.worker_claim import worker_claim

ROLES = ("router", "l1", "worker", "reviewer", "spawner", "system", "oversight",
         "reader")
KEY = "demo:A"
SHA = "0" * 40

#: ROLE_GATE action -> (the private, named; a call that passes the role check
#: and then fails or does nothing harmful on bogus arguments)
PRIVATES = {
    "log_item": ("store_fixlog.py:_log_item",
                 lambda s, p, now: s._log_item(p, KEY, {}, now)),
    "close_on_decision": ("store_close.py:_close_on_decision",
                          lambda s, p, now: s._close_on_decision(p, KEY, "b", now)),
    "apply_ops": ("store_ops.py:_apply_ops",
                  lambda s, p, now: s._apply_ops(p, [], "b", now, dry_run=True)),
    "mint_run_token": ("store_tokens.py:_mint",
                       lambda s, p, now: s._mint(p, KEY, "writer", "r1", now)),
    "rule_set": ("store_rules.py:_rule_set",
                 lambda s, p, now: s._rule_set(p, [], now)),
    "phase_plan": ("planner/phase.py:plan_phase",
                   lambda s, p, now: plan_phase(s, p, "nope:X", repo_root=".")),
    "worker_claim": ("planner/worker_claim.py:worker_claim",
                     lambda s, p, now: worker_claim(s, p, KEY, repo_root=None)),
    "set_legacy_state": ("store_legacy.py:_set_legacy_state",
                         lambda s, p, now: s._set_legacy_state(p, KEY, "x", "", now)),
    "set_kind": ("store_kind.py:_set_kind",
                 lambda s, p, now: s._set_kind(p, KEY, "x", now)),
    "rehome": ("store_rehome.py:_rehome",
               lambda s, p, now: s._rehome(p, KEY, "other", None, now)),
    "record_run": ("store_runs.py:_record_run",
                   lambda s, p, now: s._record_run(p, KEY, {}, now)),
    "answer": ("store_answer.py:_answer",
               lambda s, p, now: s._answer(p, KEY, "t", "D-1", now)),
    "phase_finish": ("planner/phase_finish.py:finish_phase",
                     lambda s, p, now: finish_phase(s, p, KEY, "bad", "bad",
                                                    repo_root=".", github=None)),
    "requeue": ("store_cards.py:_requeue",
                lambda s, p, now: s._requeue(p, KEY, "", now)),
}


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    yield world
    world.close()


def _refused_for_role(call):
    try:
        call()
    except Refused as exc:
        return exc.code == "forbidden" and (
            "may not do this" in exc.msg or " requires " in exc.msg)
    except Exception:  # noqa: BLE001 - past the role check, on bogus arguments
        return False
    return False


def test_every_role_gate_entry_has_a_named_private():
    assert set(PRIVATES) == set(ROLE_GATE)


@pytest.mark.parametrize("action", sorted(PRIVATES))
def test_role_gate_equals_the_privates_own_check(w, action):
    name, probe = PRIVATES[action]
    now = w.store.now()
    own = tuple(role for role in ROLES
                if not _refused_for_role(
                    lambda role=role: probe(
                        w.store, auth.Principal(f"x-{role}", role, ["demo"], None), now)))
    gate = tuple(role for role in ROLES if role in ROLE_GATE[action])
    assert own == gate, f"{action}: ROLE_GATE {gate} != {name} own check {own}"
