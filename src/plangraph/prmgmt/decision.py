"""Decide from supplied snapshots only; never grant or fetch operator GO."""

from dataclasses import dataclass
from enum import Enum
from typing import Literal


class Refusal(str, Enum):
    moved_head = "moved_head"
    base_not_main = "base_not_main"
    check_pending = "check_pending"
    check_failed = "check_failed"
    seat_unwitnessed = "seat_unwitnessed"
    seat_no_verdict = "seat_no_verdict"
    go_missing = "go_missing"
    go_source_unreadable = "go_source_unreadable"


@dataclass(frozen=True)
class Check:
    name: str | None
    status: str
    conclusion: str | None


@dataclass(frozen=True)
class PrState:
    number: int
    base: str
    head_sha: str
    checks: list[Check]


@dataclass(frozen=True)
class SeatRecord:
    head_sha: str
    family: str
    witnessed: bool
    verdict: Literal["ACCEPT", "CHANGES"] | None
    tests_ran: bool


@dataclass(frozen=True)
class GoRow:
    pr: int
    head_sha: str
    go_id: str
    kind: str


@dataclass(frozen=True)
class Decision:
    action: Literal["merge", "squash", "refuse"]
    refusal: Refusal | None
    detail: str


def _refuse(reason: Refusal, detail: str) -> Decision:
    return Decision("refuse", reason, detail)


def decide(
    pr: PrState,
    seats: list[SeatRecord],
    go_rows: list[GoRow],
    *,
    go_source_readable: bool,
) -> Decision:
    """First refusal wins: base, head, checks, seats, then operator GO.

    Seats count existentially on the exact head; neither their family nor
    tests_ran adds a gate in S1. A verdict is evidence, never operator GO.
    Only kind GO rows for this PR count. There is no S1 squash policy.
    """
    if pr.base != "main":
        return _refuse(Refusal.base_not_main, "PR base must be main")
    if len(pr.head_sha) != 40 or any(
        char not in "0123456789abcdefABCDEF" for char in pr.head_sha
    ):
        return _refuse(Refusal.moved_head, "PR head must be a full 40-hex SHA")

    # GitHub's unnamed pending merge-ready rollup is not an individual check.
    checks = [check for check in pr.checks if check.name is not None]
    if any(check.status != "COMPLETED" for check in checks):
        return _refuse(Refusal.check_pending, "Named checks are not all completed")
    if any(check.conclusion not in {"SUCCESS", "SKIPPED", "NEUTRAL"}
           for check in checks):
        return _refuse(Refusal.check_failed, "A named check has no passing conclusion")

    witnessed = [seat for seat in seats
                 if seat.head_sha == pr.head_sha and seat.witnessed]
    if not witnessed:
        return _refuse(Refusal.seat_unwitnessed, "No witnessed seat for the exact head")
    if not any(seat.verdict in {"ACCEPT", "CHANGES"} for seat in witnessed):
        return _refuse(Refusal.seat_no_verdict, "No witnessed seat has a verdict line")

    if not go_source_readable:
        return _refuse(Refusal.go_source_unreadable, "Operator GO source is unreadable")
    rows = [row for row in go_rows if row.pr == pr.number and row.kind == "GO"]
    if any(row.head_sha == pr.head_sha for row in rows):
        return Decision("merge", None, "All gates satisfied for the exact head")
    if rows:
        return _refuse(Refusal.moved_head, "Operator GO names a different or partial head")
    return _refuse(Refusal.go_missing, "No operator GO row for this PR")
