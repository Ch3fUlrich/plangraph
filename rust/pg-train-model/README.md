# Train model

`Model::apply` is the transition table of the landing-train actors. Each event is one atomic step of its
listed actor; the model owns no clock (`Event::Tick` ages every ready part) and no git. `invariants_hold`
is checked after every event, and a denied event changes nothing.

Objective (rule 3.74 (1), D-6744): batches merged per hour. By default every ready part rides
(`Policy::every_ready_rides`); only a part whose parent has no verified landing is left out
(`ParentNotLanded`). A red train drops the failing part (`CiRed`, with its descendants) and the rest are
retried as one group, so they land; with no named culprit the train is halved at stack boundaries
(D-6102). A size target and a bounded deferral on overlap are policy parameters, not the default.
"Trains are never merged into bigger ones" (D-6742 clause 3) is void and not modelled; "no part joins a
train whose CI has started" is (`Denied::CiStarted`).

| Actor | Events |
| --- | --- |
| Registration | `Register`, `Readmit` |
| Selection | `Select` (ordinary), `Join` (an open train only), `Tick` |
| Builder and verifier | `Bind` (build, base, policy), `StartCi`, `Rebuild`, `RecordLeftOut`, `VerifyLeftOut` |
| CI | `CiResult`, `CiRed` |
| Retry groups | `StartRetry`; groups run before any ordinary selection |
| Conflict, drop, revert | `Conflict`, `Drop`, `Revert` |
| Settlement | `Settle`, `SettleLegacy` (no included list), `VerifyOutcome` |
| Main | `MainMove` |

Dispositions and their verifier: `Overlap` is accepted when the named part rides, the paths overlap and
the part had waited less than the bound; `ParentNotLanded` when the named parent is the part's parent, does
not ride and has no verified landing (a legacy settled train verifies only by `VerifyOutcome`); `Target`
when the train is at its size target. `tests/dispositions.rs` has one accepted and one refused case each.

## Tests

- `tests/model.rs`, `tests/dispositions.rs`: the named cases, among them a red train of 14 parts whose
  retry group of seven runs although no ordinary part is due, and a legacy settled train whose dependent
  child waits for the parent's verified outcome.
- `tests/exhaustive.rs`: every ordering of the alphabet to depth 9 over four parts (an overlap, a child),
  under both orders and the default policy.
- `tests/replay.rs`: replay of trains 9 to 16 (`fixtures/trains_9_16.json`, 52 batches from git history;
  ready = committer time of the batch head, paths = files of its own diff). `--ignored --nocapture`
  prints the table below.

## Replay over trains 9 to 16 (52 batches, 8 trains)

One train at a time; a train costs 15 min + 15 min per extra part + C per resolved conflict (a part that
overlaps an earlier part of its train). The costs are assumptions applied to every rule alike. Overlap is
path-level, so it overstates real merge conflicts. The red rate is unknown: the history records no CI
result per train. Sample: 52 batches; the arrival rate caps parts per hour at about 1.2 for any rule that
does not wait.

Which deferral policy, (a) by count or (b) oldest first: (b) gave fewer or equal conflicts and a shorter
or equal longest wait than (a) in all six comparisons (C = 0, 15, 60 min; three size and bound settings
each). Example at C = 60 min, target 2, bound 1 h: (a) 9 conflicts, longest wait 7.1 h; (b) 9 conflicts,
5.9 h; at target 4, bound 2 h: (a) 6 conflicts, 6.7 h; (b) 0 conflicts, 2.8 h.

Every ready part rides, against deferral: at C = 0 it gives the highest parts per hour (1.24 against
1.21) and the shortest longest wait (2.6 h); at C = 15 min it gives 1.24 and a longest wait of 6.7 h
against 2.8 to 4.5 h; at C = 60 min it falls to 0.95 parts per hour with a longest wait of 37 h, because
the first train takes the whole backlog. Which is right depends on what a resolved conflict costs; the
sample cannot measure that.

How long an ordinary part waits behind retry groups: bounded, so no D-6102 change is needed. Retry groups
are a tree of at most 2n - 2 runs for a red train of n parts (n - 1 with a named culprit), no ordinary
selection starts while any group is pending, and new red trains cannot start meanwhile, so repeated red
trains do not stack: the invariant `retry_bounded` holds in every explored interleaving, and the waiting
part rides in the next ordinary train (`an_ordinary_part_waits_behind_a_red_train...`). Rebuilds after
`MainMove` come on top of that count.

## Limits

The plan rows of TRAIN-PARTS-DISJOINT-WITH-BOUNDED-DEFERRAL, TRAIN-RED-ESCALATION-AFTER-TWO-ROUNDS and
TRAIN-SPECULATIVE-NEXT were not readable in the writing session (no keeper access); their cases are taken
from the batch brief, D-6679 and rule 3.74. Escalation is modelled as the second red round of a lineage;
speculative next trains (a second live train) are not modelled.
