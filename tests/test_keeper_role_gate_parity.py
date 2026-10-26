"""ROLE_GATE (the seam's audited first check) equals each private's own check.

The private methods keep their own role checks for callers outside the seam
(``apply_ops`` batch dispatch, direct calls).  Each pair is compared by calling
the private with every role -- never by parsing source -- so the test fails if
either the table entry or the private's own list changes alone.

203-r5 T2: ``api_routes.INLINE_ROLES`` (the inline ``_write`` verbs' role gate)
is pinned the same way, against the STORE's own check: each probe calls the
store verb with every role, in the best position the store gives that role
(claim-holder for the claimant verbs, manager where the verb admits one), and
classifies the store's refusal -- never the API table's.
"""
import pytest

from keeper_util import World, defn
from plangraph.keeper import auth
from plangraph.keeper.api_routes import INLINE_ROLES
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


# ---------------------- INLINE_ROLES vs the store's own check (203-r5 T2)
#: verb -> {role: reason} for an entry deliberately WIDER than the store's own
#: check (the 403 the extra role then gets comes from the store instead of the
#: table, still audited -- so a superset is safe, a subset is not).  Empty:
#: every entry must equal the store's admitted roles exactly.
INLINE_SUPERSETS = {}


def _principal(w, role):
    """A stored identity of *role* scoped to ``demo``: the store re-reads the
    principal row on every verb (``_check_principal``), so probes must call
    with a real identity -- a matching row -- never a rowless
    ``auth.Principal`` (that is refused before any role check runs).

    ``oversight`` is the one row ``add_principal`` refuses (the role is minted
    only through the TTL door), so it is provisioned via
    ``add_oversight_principal`` and the probe calls with the principal that
    matches that row.  The parity pins the VERB's own role check -- the one
    ``apply_ops`` batch dispatch and direct callers hit, the reason the
    privates keep their own lists -- not the ``OversightPrincipal``-token
    policy layer (``oversight_policy.check_action``), which is pinned by
    ``test_keeper_oversight_scope``.
    """
    name = f"probe-{role}"
    if role == "oversight":
        auth.add_oversight_principal(w.conn, name, ["demo"])
        return auth.Principal(name, role, ["demo"], None)
    token = auth.add_principal(w.conn, name, role, ["demo"], None)
    return auth.authenticate(w.conn, token)


def _admitted(call):
    """True when the store's own check lets the caller past.

    Only an authorisation ``Refused`` (``forbidden``) means "not admitted";
    success and later validation/state refusals both mean the role was let in.
    """
    try:
        call()
    except Refused as exc:
        return exc.code != "forbidden"
    except Exception:  # noqa: BLE001 - past the authorisation layer
        return True
    return True


def _open_lane(w, pid, **kw):
    """One open lane plus its own goal, created by the router (setup only)."""
    w.store.create_items(w.p("router"),
                         [defn(pid, **kw), defn(pid + "G", [pid], kind="goal")])
    return f"demo:{pid}"


def _claim_as(w, p, key):
    """Position *p* as the claimant of *key*; False when its role cannot hold
    a claim at all (``store.claim`` is l1/worker, ``worker_claim_lease`` the
    system door the phase worker uses)."""
    door = w.store.worker_claim_lease if p.role == "system" else w.store.claim
    return _admitted(lambda: door(p, key))


def _identity_probe(call):
    """Probe a claimant-identity verb: give the role its best position (its own
    claim, or workerA's claim when the role can never hold one) and let the
    store's own check decide.  ``call(store, p, key)`` runs past the position
    setup; its refusal classifies the role."""
    def probe(w, role):
        key = _open_lane(w, f"m{role}")
        p = _principal(w, role)
        if not _claim_as(w, p, key):
            w.store.claim(w.p("workerA"), key)
        return _admitted(lambda: call(w.store, p, key))
    return probe


def _probe_create(w, role):
    p = _principal(w, role)
    return _admitted(lambda: w.store.create_items(
        p, [defn(f"n{role}"), defn(f"n{role}G", [f"n{role}"], kind="goal")]))


def _probe_import(w, role):
    p = _principal(w, role)
    return _admitted(lambda: w.store.import_items(
        p, [defn(f"i{role}", state="open"),
            defn(f"i{role}G", [f"i{role}"], kind="goal", state="open")]))


def _probe_edit(w, role):
    key = _open_lane(w, f"e{role}")
    op = _open_lane(w, f"o{role}", operator=True)
    p = _principal(w, role)
    if _admitted(lambda: w.store.edit_definition(p, key, {})):
        return True                            # manages the item's project
    if not _claim_as(w, p, key):
        return False                           # no position the store admits
    # claim-holder position: a depends_on addition on the role's own claim
    return _admitted(
        lambda: w.store.edit_definition(p, key, {"depends_on": [op]}))


def _probe_claim(w, role):
    p = _principal(w, role)
    return _admitted(lambda: w.store.claim(p, _open_lane(w, f"c{role}")))


def _probe_cancel(w, role):
    p = _principal(w, role)
    return _admitted(lambda: w.store.cancel(p, _open_lane(w, f"x{role}")))


def _probe_set_priority(w, role):
    p = _principal(w, role)
    return _admitted(lambda: w.store.set_priority(p, "demo:NOPE", 5))


def _probe_verdict(w, role):
    p = _principal(w, role)
    return _admitted(lambda: w.store.verdict(p, "demo:NOPE", True))


def _probe_add_evidence_router(w, role):
    p = _principal(w, role)
    # "commit+ci" is the one kind the system path admits (phase merge proof)
    return _admitted(
        lambda: w.store.add_evidence_router(p, "demo:NOPE", "commit+ci", {}))


def _probe_waive(w, role):
    p = _principal(w, role)
    return _admitted(lambda: w.store.waive_second_family(p, "demo:NOPE", "D-1"))


#: INLINE_ROLES verb -> probe(w, role): does the STORE's own check admit *role*
#: somewhere?  The probes call the store verb with every role, never the API
#: table, so this fails when either the entry or the store's check changes.
#: The claimant-identity verbs (store_verbs.py: delegate is claimant-only,
#: heartbeat/submit/add_evidence run through ``_actor_or_delegate``, release is
#: claimant-or-router) all position the role through ``_identity_probe``; the
#: epochs below are deliberately stale (a claimed lane is at epoch 1) so the
#: probe classifies the authorisation check and then stops at ``epoch_stale``.
INLINE_PROBES = {
    "create_items": _probe_create,
    "import_items": _probe_import,
    "edit_definition": _probe_edit,
    "set_priority": _probe_set_priority,
    "claim": _probe_claim,
    "delegate": _identity_probe(lambda s, p, key: s.delegate(p, key, "workerA")),
    "heartbeat": _identity_probe(lambda s, p, key: s.heartbeat(p, key, 0)),
    "release": _identity_probe(lambda s, p, key: s.release(p, key, 0)),
    "submit": _identity_probe(lambda s, p, key: s.submit(p, key, 0)),
    "add_evidence": _identity_probe(
        lambda s, p, key: s.add_evidence(p, key, 0, "commit+ci", {})),
    "add_evidence_router": _probe_add_evidence_router,
    "verdict": _probe_verdict,
    "cancel": _probe_cancel,
    "waive_second_family": _probe_waive,
}


def test_every_inline_roles_entry_has_a_probe():
    assert set(INLINE_PROBES) == set(INLINE_ROLES)


@pytest.mark.parametrize("verb", sorted(INLINE_PROBES))
def test_inline_roles_equal_the_stores_own_check(w, verb):
    probe = INLINE_PROBES[verb]
    own = tuple(role for role in ROLES if probe(w, role))
    entry = tuple(role for role in ROLES if role in INLINE_ROLES[verb])
    # positive control: the probe discriminates, it is not vacuously all/none
    assert 0 < len(own) < len(ROLES), f"{verb}: probe admits {own}"
    extra = set(entry) - set(own)
    documented = INLINE_SUPERSETS.get(verb, {})
    assert set(own) <= set(entry), (
        f"{verb}: INLINE_ROLES {entry} is narrower than the store's own"
        f" check {own}; widen the entry")
    assert not (extra - set(documented)), (
        f"{verb}: INLINE_ROLES {entry} exceeds the store's own check {own}"
        f" without a documented reason")
    assert not (set(documented) - set(entry)), (
        f"{verb}: documented superset {documented} is stale (not in the entry)")
    assert not (extra & set(own)), (
        f"{verb}: documented superset {documented} is stale"
        f" (the store admits those roles now)")
