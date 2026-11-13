"""Only canonical D-NNN waiver identifiers may loosen review gates."""
import pytest

from keeper_util import to_in_review
from plangraph.keeper.errors import Refused
from test_keeper_waiver import KEY, _count, w


@pytest.mark.parametrize("state", ["claimed", "in_review"])
@pytest.mark.parametrize("decision_id", [
    "d-217", " D-217", "D-217 ", "D-217\n", "\tD-217", "217",
    "waived", "D-", "D-217-extra", "D-٢١٧", "", None, 217,
])
def test_waive_requires_canonical_decision_id(w, state, decision_id):
    if state == "claimed":
        w.store.claim(w.p("l1main"), KEY)
    else:
        to_in_review(w, KEY)
    before = w.store._item(KEY)
    calls = len(w.verifiers["decision"].calls)
    with pytest.raises(Refused) as exc:
        w.store.waive_second_family(w.p("router"), KEY, decision_id)
    assert exc.value.code == "validation"
    assert exc.value.msg == "decision_id must look like D-NNN"
    assert w.store._item(KEY) == before
    assert len(w.verifiers["decision"].calls) == calls
    assert _count(w, KEY, "waiver_set") == 0
    assert _count(w, KEY, "review_verdict") == 0
    assert _count(w, KEY, "review_passed") == 0
    assert w.store.verify_chain() == []
    assert w.store.verify_integrity() == []


@pytest.mark.parametrize("waiver", [
    "d-217", " D-217", "D-217 ", "D-217\n", "\tD-217", "217",
    "waived", "D-", "D-217-extra", "D-٢١٧", "", None, 217, True, [], {},
])
def test_malformed_stored_waiver_is_not_active(w, waiver):
    assert not w.store._waiver_active({"review_waiver": waiver})


@pytest.mark.parametrize("waiver", ["D-0", "D-217", "D-0217"])
def test_canonical_stored_waiver_is_active(w, waiver):
    assert w.store._waiver_active({"review_waiver": waiver})
