from dataclasses import FrozenInstanceError, replace

import pytest

from plangraph.prmgmt.decision import (
    Check, Decision, GoRow, PrState, Refusal, SeatRecord, decide,
)


HEAD = "a" * 40
OLD = "b" * 40


def state():
    return PrState(42, "main", HEAD, [Check("unit", "COMPLETED", "SUCCESS", HEAD)])


def seat(**changes):
    return replace(SeatRecord(HEAD, "reviewer", True, "ACCEPT", True), **changes)


def go(sha=HEAD):
    return GoRow(42, sha, "operator-go", "GO")


def run(pr=None, seats=None, rows=None, readable=True):
    return decide(
        state() if pr is None else pr,
        [seat()] if seats is None else seats,
        [go()] if rows is None else rows,
        go_source_readable=readable,
    )


def refuses(result, reason):
    assert result.action == "refuse"
    assert result.refusal is reason
    assert result.detail


def test_happy_path_merges():
    assert run().action == "merge"
    assert run().refusal is None


@pytest.mark.parametrize("head", [HEAD[:12], "z" * 40, "", HEAD + "a"])
def test_moved_head(head):
    refuses(run(pr=replace(state(), head_sha=head)), Refusal.moved_head)


def test_unfinished_check():
    pr = replace(state(), checks=[Check("unit", "IN_PROGRESS", None, HEAD)])
    refuses(run(pr=pr), Refusal.check_pending)


@pytest.mark.parametrize("conclusion", ["FAILURE", "CANCELLED", "TIMED_OUT", None])
def test_failed_check(conclusion):
    pr = replace(state(), checks=[Check("unit", "COMPLETED", conclusion, HEAD)])
    refuses(run(pr=pr), Refusal.check_failed)


def test_unwitnessed_seat():
    refuses(run(seats=[seat(witnessed=False)]), Refusal.seat_unwitnessed)


def test_seat_without_verdict():
    refuses(run(seats=[seat(verdict=None)]), Refusal.seat_no_verdict)


def test_missing_go_row():
    refuses(run(rows=[]), Refusal.go_missing)


def test_go_for_older_sha():
    refuses(run(rows=[go(OLD)]), Refusal.moved_head)


def test_prefix_sha_never_matches():
    refuses(run(rows=[go(HEAD[:12])]), Refusal.moved_head)
    refuses(run(seats=[seat(head_sha=HEAD[:12])]), Refusal.seat_unwitnessed)


def test_unreadable_go_source_never_bypasses():
    refuses(run(readable=False), Refusal.go_source_unreadable)
    refuses(run(rows=[], readable=False), Refusal.go_source_unreadable)


def test_null_name_pending_rollup_excluded():
    pr = replace(state(), checks=state().checks + (Check(None, "PENDING", None, HEAD),))
    assert run(pr=pr).action == "merge"


@pytest.mark.parametrize("conclusion", ["SUCCESS", "SKIPPED", "NEUTRAL"])
def test_allowed_check_conclusions(conclusion):
    pr = replace(state(), checks=[Check("unit", "COMPLETED", conclusion, HEAD)])
    assert run(pr=pr).action == "merge"


def test_seats_are_exact_head_and_existential():
    refuses(run(seats=[seat(head_sha=OLD)]), Refusal.seat_unwitnessed)
    refuses(run(seats=[]), Refusal.seat_unwitnessed)
    assert run(seats=[seat(witnessed=False), seat()]).action == "merge"
    refuses(run(seats=[seat(witnessed=False), seat(verdict=None)]),
            Refusal.seat_no_verdict)


def test_verdict_is_not_go_and_tests_ran_is_not_a_gate():
    assert run(seats=[seat(tests_ran=False)]).action == "merge"
    refuses(run(seats=[seat(verdict="ACCEPT")], rows=[]), Refusal.go_missing)


def test_go_requires_correct_pr_and_kind():
    refuses(run(rows=[replace(go(), pr=43)]), Refusal.go_missing)
    refuses(run(rows=[replace(go(), kind="ACCEPT")]), Refusal.go_missing)
    assert run(rows=[go(OLD), go()]).action == "merge"


def test_refusal_order():
    bad = replace(state(), base="develop", head_sha="short",
                  checks=[Check("unit", "PENDING", None, HEAD)])
    refuses(run(pr=bad, seats=[], rows=[], readable=False), Refusal.base_not_main)
    refuses(run(pr=replace(bad, base="main")), Refusal.moved_head)
    refuses(run(pr=replace(bad, base="main", head_sha=HEAD), seats=[], rows=[]),
            Refusal.check_pending)
    refuses(run(seats=[], rows=[], readable=False), Refusal.seat_unwitnessed)


def test_pending_precedes_failed_independent_of_check_order():
    checks = [Check("failed", "COMPLETED", "FAILURE", HEAD),
              Check("pending", "QUEUED", None, HEAD)]
    for ordered in (checks, list(reversed(checks))):
        refuses(run(pr=replace(state(), checks=ordered)), Refusal.check_pending)


@pytest.mark.parametrize("obj", [state(), seat(), go(), Check(None, "PENDING", None, HEAD),
                                 Decision("merge", None, "ready")])
def test_records_are_frozen(obj):
    field = next(iter(obj.__dataclass_fields__))
    with pytest.raises(FrozenInstanceError):
        setattr(obj, field, None)


@pytest.mark.parametrize("checks", [[], [Check(None, "PENDING", None, HEAD)],
                                  [Check(None, "COMPLETED", "SUCCESS", HEAD)]])
def test_missing_named_checks(checks):
    refuses(run(pr=replace(state(), checks=checks)), Refusal.check_missing)


@pytest.mark.parametrize("conclusion", ["FAILURE", "CANCELLED", "TIMED_OUT", None])
def test_unnamed_completed_failure(conclusion):
    checks = state().checks + (Check(None, "COMPLETED", conclusion, HEAD),)
    refuses(run(pr=replace(state(), checks=checks)), Refusal.check_failed)


@pytest.mark.parametrize("name,status", [("unit", "COMPLETED"), (None, "PENDING")])
def test_stale_check_precedes_check_and_seat_gates(name, status):
    checks = (Check(name, status, "FAILURE", OLD),)
    refuses(run(pr=replace(state(), checks=checks), seats=[], rows=[]),
            Refusal.moved_head)
    refuses(run(pr=replace(state(), base="develop", checks=checks)),
            Refusal.base_not_main)


@pytest.mark.parametrize("accept_first", [False, True])
def test_witnessed_changes_veto_accept(accept_first):
    seats = [seat(verdict="CHANGES"), seat()]
    if accept_first:
        seats.reverse()
    refuses(run(seats=seats, rows=[], readable=False), Refusal.seat_changes_requested)
    refuses(run(seats=[seat(verdict="CHANGES", tests_ran=False)]),
            Refusal.seat_changes_requested)


def test_only_witnessed_exact_head_changes_veto():
    assert run(seats=[seat(), seat(verdict="CHANGES", witnessed=False),
                      seat(verdict="CHANGES", head_sha=OLD)]).action == "merge"


def test_checks_snapshot_is_immutable():
    checks = list(state().checks)
    pr = replace(state(), checks=checks)
    checks.clear()
    assert pr.checks == state().checks
    with pytest.raises(TypeError):
        pr.checks[0] = Check("unit", "COMPLETED", "FAILURE", HEAD)
    with pytest.raises(FrozenInstanceError):
        pr.checks += (Check("extra", "PENDING", None, HEAD),)
    assert run(pr=pr).action == "merge"
