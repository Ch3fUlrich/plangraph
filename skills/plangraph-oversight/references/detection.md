# Detection checks, every beat

Run these after the beat lines of `SKILL.md`. Each check names what to read, the signal and
the action. A signal is a finding: act on it the same beat (fix, re-dispatch, or file an
incident on its class item), never wait for the next round to see whether it clears.

## Contents
1. Run endings by layer
2. A writer told to commit again
3. Flaky tests on unrelated heads
4. Build capacity
5. Review seats
6. Claims that lapse
7. Quota runway
8. Duplicate work
9. Stale waits
10. Changes on the same path

## 1. Run endings by layer

Read every run that ended since the last beat: `status`, `reason` and `completion` in its
`record.json`, and the writer's last message (`native.last`) in the run directory. Sort each
ending into a layer before you act:

| Reason seen | Usual real cause | Layer |
|---|---|---|
| `no_commit` with "head is final", "slot busy", "lease held" in the last message | gate could not run | infra-capacity |
| `fast checks failed` whose only error is a slot or lease wait | gate could not run | infra-capacity |
| `weak_verdict` with "two distinct review families unavailable" | no review seat | infra-seat |
| `harness_limit`, `limited_until`, HTTP 429 | provider quota | infra-quota |
| `base_stale: behind=N` | main moved | infra-host (rebase) |
| `stalled_no_leg`, `run_terminated_before_leg` | dispatch or lease wait | infra-capacity |
| sandbox, PowerShell, read-only file system, socket errors | host or sandbox | infra-host |
| a test or review finding in the change | the change | change-check / change-review |
| "needs authorization for <path>" | write set too small | writer-scope |

Act on infra layers at the infra: free or add the slot, seat a reviewer of an allowed family,
rebase, grant the scope. Resume the writer only for change and scope layers. Count endings per
layer per day; an infra share above a third of endings is the first item of the next batch.

## 2. A writer told to commit again

Two endings of one run with the same head and no new commit mean the driver asked for a
commit the writer could not make. Read why from its last message and fix that cause; a
follow-up that only repeats the request burns a writer turn and its whole context.

## 3. Flaky tests on unrelated heads

A fast-check or CI failure in a crate or test file the head does not touch is a flaky test
until proven otherwise. Record it as an incident on that test's class item (or the flaky-test
class item), rerun the check, and never send it to the writer as a finding. A test that fails
three different heads in seven days goes on the quarantine list of its item.

## 4. Build capacity

Per work host: pool slots and their lease holders, free space on the filesystem that holds the
cargo target, the size of the primary target between beats (a drop during builds means the
cache was trimmed in use), fast-check wall time against its budget. Slots below what disk and
memory allow, a slot held by a process that has ended, or wall time over budget on two heads
in a row is a finding with the measured numbers.

## 5. Review seats

For each writer family in flight, ask the route which review legs it would seat for that
family (role review, the writer's family excluded) and read each leg's state. An empty list
means every head of that family will end without a verdict: seat the allowed fallback seat
explicitly with `--leg` and report the gap the same beat.

## 6. Claims that lapse

Compare each orchestrator's claimed items with its live runs and lease ends. A lease ending
before the next beat with a live run behind it, or a renewal unit that has not run within its
interval, is a finding: renew now and name the stopped unit.

## 7. Quota runway

Read the remaining share of each model pool and its change since the last beat; divide by the
burn per hour for the hours left. Runway shorter than the time to the reset decides the next
spawn: spend what is left on work closest to landing, never on optional rounds.

## 8. Duplicate work

Before you brief a spec writer or a writer, search main and every open lane for the item's
spec and commits (`git log --all --oneline -- docs/specs/<ID>.md`, `git log --all --grep <ID>`).
Work that exists is merged or fixed, not redone.

## 9. Stale waits

A run, unit, pull request, train or GO request with no state change for two beats is a
finding. Find what it waits on and unblock it or report who must.

## 10. Changes on the same path

When two or more changes to one path (build, leases, disk hygiene, host moves, routes) went
live since the last beat and failures rose, treat the changes as the first suspect and roll
the newest back or isolate it on one host before anything else.
