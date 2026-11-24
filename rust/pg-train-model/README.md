# Train model

`Model::apply` is the transition table of the landing-train actors. Each event is one atomic step of its
listed actor; the model owns no clock (`Event::Tick` ages every ready part) and no git. `invariants_hold`
is checked after every event, and a denied event changes nothing.

Objective (rule 3.74 (1), D-6744): batches merged per hour. By default every ready part rides
(`Policy::every_ready_rides`); only a part whose parent has no verified landing is left out
(`ParentNotLanded`). A red train drops the failing part (`CiRed`, with its descendants) and the rest are
retried as one group, so they land; with no named culprit the train is halved at stack boundaries
(D-6102). A size target, a cap and a bounded deferral on overlap are policy parameters, not the default.
"Trains are never merged into bigger ones" (D-6742 clause 3) is void and not modelled; "no part joins a
train whose CI has started" is (`Denied::CiStarted`).

| Actor | Events |
| --- | --- |
| Registration | `Register` (refused for the child of a reverted part), `Readmit` |
| Selection | `Select` (ordinary), `Join` (an open train only), `Tick` |
| Builder and verifier | `Bind` (build, base, policy), `StartCi`, `Rebuild`, `Amend`, `Restart`, `RecordLeftOut`, `OmitLeftOut`, `VerifyLeftOut`, `VerifyComplete` |
| CI | `CiResult`, `CiRed`, `CiRedFix` and `FixInPlace` (the first red round only) |
| Retry groups | `StartRetry`; groups run before any ordinary selection |
| Speculative next train | `Speculate`, `Promote` (tested fast-forward only) |
| Conflict, drop, revert | `Conflict`, `Adopt` (a resolution, refused once CI has started), `Drop`, `Revert` |
| Settlement | `Settle`, `SettleLegacy` (no included list), `VerifyOutcome` |
| Main | `MainMove` |

Dispositions of a ready part that does not ride: `Overlap` (names the part; deferred while its wait is
under the bound), `ParentNotLanded`, `Target`, `Cap`, `Conflict`, `Dropped`, `Descendant`, and retry (a held part).
Each has a verifier rule that accepts it only when its facts hold; `VerifyComplete` refuses a ready part
with neither a place nor a reason. A legacy settled train (no included list) verifies nothing: its child
waits for `VerifyOutcome`. After two red rounds the builder stops fixing in place; a landing whose tree is
not the tested tree is refused; a speculative candidate is discarded by any change to its predecessor or to
main, is never landing evidence in a pull request merge mode, and lands only when main's tip is exactly its
predecessor.

## Tests

- `tests/model.rs`, `red_trains.rs`, `selection.rs`, `parts.rs`, `builder.rs`, `speculative.rs`,
  `dispositions.rs`: the named cases of the plan rows, among them nine parts on one path landing as 1, 1
  and 7 under policy (a), 101 due parts giving a train of 100 and one `Cap`, a red train of 14 parts whose
  retry group of seven runs although no ordinary part is due, a legacy settled train whose child waits for
  the verified outcome, the child of a reverted part refused at registration and at build, a stack of three
  or more dropped or reverted while held, a restart that keeps deferral age and red rounds.
- `tests/exhaustive.rs`: every ordering of the alphabet to depth 8 over four parts (a stack of three with
  an overlap, and an independent part), under policy (a), policy (b) and the default.
- `tests/replay.rs`: replay of trains 9 to 16 (`fixtures/trains_9_16.json`, 52 batches from git history;
  ready = committer time of the batch head, paths = indices of the files of its own diff). The ignored test
  prints the table below.

## Replay over trains 9 to 16 (52 batches, 8 trains)

One train at a time; a train costs 15 min + 15 min per extra part + C per resolved conflict (a part that
overlaps an earlier part of its train). The costs are assumptions applied to every rule alike. Overlap is
path-level, so it overstates real merge conflicts. The red rate is unknown: the history records no CI
result per train. Sample: 52 batches; the arrival rate caps parts per hour at about 1.2 for any rule that
does not wait.

Which deferral policy, (a) by count (a part left out twice is due and goes first) or (b) oldest first:
(b) gave fewer or equal conflicts than (a) in all nine comparisons (C = 0, 15, 60 min; targets 2, 4, 8
with bounds 1, 2, 4 h) and a longest wait that was equal or shorter in seven of nine; in the other two (C = 0,
targets 4 and 8) (a) was 0.1 h shorter. At C = 60 min, target 4, bound 2 h: (a) 20 conflicts, longest wait
12.3 h; (b) 0 conflicts, 2.8 h. On this sample the replay picks (b).

Every ready part rides, against deferral: at C = 0 it gives the highest parts per hour (1.24 against 1.21)
and a longest wait of 2.6 h; at C = 15 min 1.24 and 6.7 h against 2.8 to 5.5 h; at C = 60 min it falls to
0.95 parts per hour with a longest wait of 37 h, because the first train takes the whole backlog. Which is
right depends on what a resolved conflict costs; the sample cannot measure that.

Speculative next train (`speculative` in the replay: the next candidate is cut when the previous train
starts, trains still land one after the other): the same parts per hour as the sequential replay wherever
arrivals are the limit, and 1.24 instead of 0.95 (every ready part rides) and 1.21 instead of 1.14 ((a),
target 8) at C = 60 min. Main only ever advances to a train whose CI finished, after its predecessor.

How long an ordinary part waits behind retry groups: bounded, so no D-6102 change is needed. Retry groups
are a tree of at most 2n - 2 runs for a red train of n parts (n - 1 with a named culprit), no ordinary
selection or candidate starts while any group is pending, and new red trains cannot start meanwhile, so
repeated red trains do not stack: the invariant `retry_bounded` holds in every explored interleaving, and
the waiting part rides in the next ordinary train. Rebuilds after `MainMove` come on top of that count.

## Limits

Escalation is modelled as two red rounds of one train or of its lineage (`CiRedFix` counts a round; a
group's red counts the rounds before it). The durable journal is represented by what `Restart` leaves
untouched, not by a store. Replay costs are assumptions and the red rate is unknown.
