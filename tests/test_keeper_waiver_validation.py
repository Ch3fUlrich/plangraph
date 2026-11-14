"""Only canonical D-NNN waiver identifiers may loosen review gates."""
import pytest

from keeper_util import to_in_review
from plangraph.keeper.errors import Refused
from test_keeper_waiver import KEY, _count, w
from test_keeper_close_merged import HEAD, unseated_w
from types import SimpleNamespace


def historical_waiver_fixture(world, monkeypatch, *, authority="OPERATOR", date="2026-11-12"):
    world.decision_path.write_text(
        "| ID | PROJECT | DECISION | CLASS | DATE |\n"
        "| --- | --- | --- | --- | --- |\n"
        f"| D-5504 | demo | GO #1; GO-HEAD {HEAD} | ROUTER | {date} |\n"
        f"| D-6000 | demo | Waiver: merged-without-witnesses; Waives: demo:H | {authority} | 2026-11-14 |\n")
    git = world.store.ctx["git"]
    original = git._run
    monkeypatch.setattr(git, "_run", lambda repo, args: SimpleNamespace(
        returncode=0, stdout=f"{date}T12:00:00Z") if "--format=%cI" in args else original(repo, args))
    monkeypatch.setattr("plangraph.keeper.context_sources.open_lanes", lambda *a: [])


@pytest.mark.parametrize("decision,authority", [(None, "OPERATOR"), ("", "OPERATOR"),
                                               ("D-9999", "OPERATOR"), ("D-6000", "ROUTER")])
def test_merged_seat_waiver_refuses_without_operator_authority(unseated_w, monkeypatch, decision, authority):
    world = unseated_w
    historical_waiver_fixture(world, monkeypatch, authority=authority)
    before = world.store._evidence("demo:H")
    with pytest.raises(Refused):
        world.store.close_merged(world.p("router"), "demo:H", "D-5504", waive_seats=decision)
    assert world.store._item("demo:H")["state"] == "in_review"
    assert world.store._evidence("demo:H") == before


def test_operator_named_merged_seat_waiver_closes_with_evidence(unseated_w, monkeypatch):
    world = unseated_w
    historical_waiver_fixture(world, monkeypatch)
    result = world.store.close_merged(world.p("router"), "demo:H", "D-5504", waive_seats="D-6000")
    assert result["state"] == "done" and result["reviews_passed"] == 0
    ref = world.store._evidence("demo:H")[-1]["ref"]
    assert ref["waiver"] == {"class": "merged-without-witnesses", "decision_id": "D-6000"}
    assert ref["prs"] == [{"pr": 1, "head": HEAD, "decision_id": "D-5504"}]
    assert world.store.verify_integrity() == []


def test_historical_waiver_cli_requires_id_and_operator_class(unseated_w, monkeypatch, tmp_path):
    from door_util import Doors
    world = unseated_w
    historical_waiver_fixture(world, monkeypatch, authority="ROUTER")
    door = Doors(world, tmp_path)
    args = ("close-merged", "demo:H", "D-5504", "--waive-seats")
    try:
        with pytest.raises(SystemExit):
            door.pg("router", *args)
        code, _, err = door.pg("router", *args, "D-6000")
        assert code == 2 and "operator_seat_waiver_invalid" in err
        historical_waiver_fixture(world, monkeypatch)
        code, out, err = door.pg("router", *args, "D-6000")
        assert code == 0, err
        assert "done" in out
    finally:
        door.close()


@pytest.mark.parametrize("fault", ["cutoff", "wrong-name", "wrong-scope", "revoked", "duplicate", "denied"])
def test_merged_waiver_scope_date_and_lock_rechecks(unseated_w, monkeypatch, fault):
    from test_keeper_close_merged import _at_write_lock
    world = unseated_w
    historical_waiver_fixture(world, monkeypatch, date="2026-11-13" if fault == "cutoff" else "2026-11-12")
    text = world.decision_path.read_text()
    before = world.store._evidence("demo:H")
    if fault == "wrong-name":
        text = text.replace("merged-without-witnesses", "unrelated-waiver")
    elif fault == "wrong-scope":
        text = text.replace("Waives: demo:H", "Waives: demo:OTHER")
    elif fault == "duplicate":
        text += "| D-6000 | demo | copied waiver | OPERATOR | 2026-11-14 |\n"
    elif fault == "denied":
        text = text.replace("| Waiver:", "| REFUSED Waiver:")
    elif fault == "revoked":
        _at_write_lock(world, monkeypatch, lambda: world.decision_path.write_text(
            text.replace("| OPERATOR |", "| ROUTER |")))
    world.decision_path.write_text(text)
    with pytest.raises(Refused):
        world.store.close_merged(world.p("router"), "demo:H", "D-5504", waive_seats="D-6000")
    assert world.store._item("demo:H")["state"] == "in_review"
    assert world.store._evidence("demo:H") == before


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
