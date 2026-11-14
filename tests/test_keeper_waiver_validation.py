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
        f"| D-6000 | demo | Waiver: merged-without-witnesses; Waives-seats: demo:H | {authority} | 2026-11-14 |\n")
    git = world.store.ctx["git"]
    original = git._run
    monkeypatch.setattr(git, "_run", lambda repo, args: SimpleNamespace(
        returncode=0, stdout=f"{date}T12:00:00Z") if "--format=%cI" in args else original(repo, args))
    monkeypatch.setattr("plangraph.keeper.context_sources.open_lanes", lambda *a: [])


@pytest.mark.parametrize("decision,authority,cause", [
    ("not-a-decision", "OPERATOR", "decision_id"), ("", "OPERATOR", "decision_id"),
    ("D-9999", "OPERATOR", "unique_row_and_columns"),
    ("D-6000", "ROUTER", "operator_authority")])
def test_merged_seat_waiver_refuses_without_operator_authority(unseated_w, monkeypatch, decision, authority, cause):
    world = unseated_w
    historical_waiver_fixture(world, monkeypatch, authority=authority)
    before = world.store._evidence("demo:H")
    with pytest.raises(Refused) as exc:
        world.store.close_merged(world.p("router"), "demo:H", "D-5504", waive_seats=decision)
    assert exc.value.msg == f"operator_seat_waiver_invalid_or_ineligible: {cause}"
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
        text = text.replace("Waives-seats: demo:H", "Waives-seats: demo:OTHER")
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


@pytest.mark.parametrize("scope", [
    "demo:OTHER", "demo:HH", "prefix-demo:H", "demo:H-suffix",
    "demo:OTHER; Mention: demo:H", "demo:H,demo:OTHER",
])
def test_merged_waiver_key_outside_seat_list_refuses(unseated_w, monkeypatch, scope):
    world = unseated_w
    historical_waiver_fixture(world, monkeypatch)
    world.decision_path.write_text(world.decision_path.read_text().replace(
        "Waives-seats: demo:H", f"Waives-seats: {scope}"))
    before = world.store._evidence("demo:H")
    with pytest.raises(Refused, match="operator_seat_waiver_scope_mismatch"):
        world.store.close_merged(world.p("router"), "demo:H", "D-5504", waive_seats="D-6000")
    assert world.store._item("demo:H")["state"] == "in_review"
    assert world.store._evidence("demo:H") == before


def test_merged_waiver_merge_at_cutoff_refuses(unseated_w, monkeypatch):
    world = unseated_w
    historical_waiver_fixture(world, monkeypatch, date="2026-11-13")
    # Keep the GO date eligible, isolating the merge-commit timestamp.
    world.decision_path.write_text(world.decision_path.read_text().replace(
        "| ROUTER | 2026-11-13 |", "| ROUTER | 2026-11-12 |"))
    before = world.store._evidence("demo:H")
    with pytest.raises(Refused, match="operator_seat_waiver_merge_at_or_after_cutoff"):
        world.store.close_merged(world.p("router"), "demo:H", "D-5504", waive_seats="D-6000")
    assert world.store._item("demo:H")["state"] == "in_review"
    assert world.store._evidence("demo:H") == before


def test_merged_waiver_go_for_another_head_refuses(unseated_w, monkeypatch):
    world = unseated_w
    historical_waiver_fixture(world, monkeypatch)
    world.decision_path.write_text(world.decision_path.read_text().replace(HEAD, "f" * 40))
    before = world.store._evidence("demo:H")
    with pytest.raises(Refused, match="go_row_head_mismatch"):
        world.store.close_merged(world.p("router"), "demo:H", "D-5504", waive_seats="D-6000")
    assert world.store._item("demo:H")["state"] == "in_review"
    assert world.store._evidence("demo:H") == before


def test_merged_waiver_space_separated_scope_closes(unseated_w, monkeypatch):
    world = unseated_w
    historical_waiver_fixture(world, monkeypatch)
    world.decision_path.write_text(world.decision_path.read_text().replace(
        "Waives-seats: demo:H", "Waives-seats: demo:OTHER demo:H demo:LAST"))
    result = world.store.close_merged(world.p("router"), "demo:H", "D-5504", waive_seats="D-6000")
    assert result["state"] == "done"
    assert world.store.verify_integrity() == []


@pytest.mark.parametrize("rule,merge_date,refusal", [
    ("| 3.37 | Historical cutoff: 2026-11-12 |", "2026-11-12",
     "operator_seat_waiver_merge_at_or_after_cutoff"),
    ("| 3.37 | Historical cutoff: 2026-11-14 |", "2026-11-13", None),
    ("| 3.37 | Historical cutoff: missing |", "2026-11-12",
     "operator_seat_waiver_invalid_or_ineligible"),
    ("| 3.37 | Historical cutoff: 2026-02-30 |", "2026-11-12",
     "operator_seat_waiver_invalid_or_ineligible"),
    ("| 3.37 | 2026-11-12 or 2026-11-14 |", "2026-11-12",
     "operator_seat_waiver_invalid_or_ineligible"),
    ("| 3.37 | 2026-11-14 |\n| 3.37 | 2026-11-14 |", "2026-11-12",
     "operator_seat_waiver_invalid_or_ineligible"),
])
def test_merged_waiver_reads_rule_cutoff(unseated_w, monkeypatch, rule, merge_date, refusal):
    world = unseated_w
    historical_waiver_fixture(world, monkeypatch, date=merge_date)
    world.decision_path.write_text(world.decision_path.read_text() + rule + "\n")
    if refusal:
        before = world.store._evidence("demo:H")
        with pytest.raises(Refused, match=refusal):
            world.store.close_merged(world.p("router"), "demo:H", "D-5504", waive_seats="D-6000")
        assert world.store._item("demo:H")["state"] == "in_review"
        assert world.store._evidence("demo:H") == before
    else:
        assert world.store.close_merged(world.p("router"), "demo:H", "D-5504",
                                        waive_seats="D-6000")["state"] == "done"


@pytest.mark.parametrize("change", ["scope", "cutoff"])
def test_merged_waiver_rechecks_scope_and_cutoff_under_lock(unseated_w, monkeypatch, change):
    from test_keeper_close_merged import _at_write_lock
    world = unseated_w
    historical_waiver_fixture(world, monkeypatch)
    text = world.decision_path.read_text()
    updated = (text.replace("Waives-seats: demo:H", "Waives-seats: demo:OTHER")
               if change == "scope" else text + "| 3.37 | cutoff: 2026-11-12 |\n")
    _at_write_lock(world, monkeypatch, lambda: world.decision_path.write_text(updated))
    before = world.store._evidence("demo:H")
    reason = ("operator_seat_waiver_scope_mismatch" if change == "scope"
              else "operator_seat_waiver_merge_at_or_after_cutoff")
    with pytest.raises(Refused, match=reason):
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
