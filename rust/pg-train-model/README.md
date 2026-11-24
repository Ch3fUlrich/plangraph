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
| CI | `CiResult`, `CiRed`, `CiRedFix` (each names the run it belongs to; a result of an older run is refused) and `FixInPlace` (the first red round only) |
| Retry groups | `StartRetry`; groups run before any ordinary selection |
| Speculative next train | `Speculate`, `Promote` (tested fast-forward only) |
| Conflict, drop, revert | `Conflict`, `Adopt` (a resolution, refused once CI has started), `Drop`, `Revert` |
| Settlement | `Settle`, `SettleLegacy` (no included list), `VerifyOutcome` |
| Main | `MainMove` |

Dispositions of a ready part that does not ride: `Overlap` (names the part; deferred while its wait is
under the bound), `ParentNotLanded`, `Target`, `Cap`, `Conflict`, `Dropped`, `Descendant`, and retry (a held part).
Each has a verifier rule that accepts it only when its facts hold; `VerifyComplete` runs every rule and
refuses a ready part with neither a place nor a reason, and CI and settlement need its result for the
train as it stands (any later change to members, reasons or tree unverifies it). `Join` applies the
selection rule, so a part deferred for overlap joins only once it is due, and a held child starts only on
a parent whose outcome is verified. A legacy settled train (no included list) verifies nothing: its child
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
- `tests/exhaustive.rs`: every ordering of the alphabet to depth 6 from three starting points (a fresh model, a train under test with a part waiting, two parts under test) over four parts (a stack of three with
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

How long an ordinary part waits behind retry groups: bounded in groups, not in time. Retry groups are a
tree of at most 2n - 2 runs for a red train of n parts (n - 1 with a named culprit), and no ordinary
selection or candidate starts while any group is pending (D-6102). But the bound counts groups, not CI
restarts: every move of main makes the running retry train stale, a stale train is rebuilt and tested
again, and the invariant counts one run for it. Three moves of main gave four CI runs of the first retry
train while the ordinary part stayed ready (`main_moves_restart_a_retry_train_without_a_bound...`), so
with main moving without end the wait has no bound. A rule that would bound it changes D-6102 and goes
to the operator: either (a) after k rebuilds of one retry train (k = 2, say) the next ordinary selection
is allowed to run beside it, or (b) a retry train that went stale k times lands on a tested
fast-forward in place of being rebuilt.

Ruling D-6840 (routing, an EXPERIMENT): option (b) with k = 2 in tested fast-forward, option (a) with k = 2
in pull request merge mode (the live path, rule 3.44 (3)); `Policy::stale_k` is k, coded by `Policy::landing`.
In tested fast-forward a retry train rebuilt twice because its base went stale is not rebuilt a third time
(`Denied::StaleBound`): its green result lands on the main as it is now (`Model::stale_landings`), but only
when what main gained since its tested base touches none of its paths (`Event::MainMove { paths }` and the
landings carry theirs), so the tested tree is what a fast-forward of the current main would hold; a move
that touches them makes a rebuild necessary and it is not refused. Rebuilds on an unchanged base do not
count. In pull request merge mode, once the live retry train was rebuilt twice an ordinary selection runs
beside it; that ordinary train goes on after the retry train closes, and the queued retry groups start when
it has landed or gone red. `stale_k` 0 (`without_stale_bound`)
is the plain per-group bound; the test `with_the_plain_per_group_bound_main_moves_restart_a_retry_train_without_end_*`
keeps the three moves of main giving four runs under it.

What the experiment records, per retry train and per day, from the live landing journal (the model counts
runs and rebuilds, not minutes): CI runs of the retry train (`run`; model: 3 at most in tested fast-forward,
4 and more under the plain bound), minutes an ordinary part waited from ready to its train's start (model:
behind two rebuilds, not behind the main-move rate), and red main after a stale-k landing (a landing that
went stale; model: `stale_landings`). At the first red main after a stale-k landing, set `stale_k` to 0 and
return to the plain per-group bound.

## Limits

Escalation is modelled as two red rounds of one train or of its lineage (`CiRedFix` counts a round; a
group's red counts the rounds before it). The durable journal is represented by what `Restart` leaves
untouched, not by a store. Replay costs are assumptions and the red rate is unknown.
