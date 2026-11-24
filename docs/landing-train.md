# Landing train builder

`plangraph land-train build` builds one landing train from the READY parts the registrar hands it
(rust/plangraph-cli/src/land: `registrar`, `train`, `build`, `git`, `cli`). No model turn is involved.

- **Registrar rules** (`registrar.rs`): every observed head is pinned under `land/seen/<pr>/<head>`, a READY head also
  under `land/ready/<pr>/<head>`. A pull request whose base ref is another open pull request's head branch is its child:
  it registers only while its head contains the parent's READY head (`behind_parent` otherwise) and carries no commit of an
  earlier parent head (`superseded_parent_history`). A child of a landed parent registers as a bottom part on the landed
  head. A head already in the landing base is `contained`. The entry-predicate conditions (`imported_history`,
  `undeclared_dependency`, `shared_history`, `.gitattributes`) belong to the part-entry checks, not to this code.
- **Closure** (`train.rs`): a part enters only after its parent with exactly the registered head, or when that head is in the
  train base. A part left out takes its descendants with it. A stack above the cap enters as a prefix; the rest is left out as
  `cap`, a failed merge as `conflict`, a part below a left-out part as `descendant`. Conflicts are not resolved here.
- **Manifest** (`build.rs`): base, parts in stack order with their own-commit digest, dependency graph, Git version, the merge
  attributes used, the merge commit and review receipt of each part, every READY head left out with its reason. The READY
  snapshot names the previously checked seat as `receipt: {run_id, head}`. The builder requires its exact part head and
  replay checks that the manifest carries the receipt from the independently supplied READY snapshot. Merges run in a private shared
  clone with no user, system or repository configuration and no `info/attributes`; attributes come only from the recorded file.
- **Verification** (`verify_manifest`, `verify_result`): the registered head and parent of every part, the whole first-parent
  chain (one merge per part with exactly the two expected parents), a clean replay of every merge under the recorded inputs, and
  the exact set of introduced commits. A build result must account for every part of its snapshot; a `conflict` is repeated at
  the prefix the verifier derives, a `descendant` is proven from the dependencies.
- **Freeze** (`check_freeze`): the build opens with policy version, base and grant digest; the freeze must carry the same three
  and the same full selection.
- **No train on a red main**: `build` asks `plangraph main-state` first and refuses with `main_red`, `main_pending`,
  `state_unknown` or `ref_not_forward_only`.

- **Resolution by request** (`resolve.rs`, conflict note V12 to V15): the append rules of the keeper's resolve events as
  a state machine. A request (unique per build position, deadline 30 minutes) has at most one open submission; a
  correction from the batch's recorded writer before the deadline names the open one and supersedes it, and a superseded
  submission can no longer be secured, rejected or adopted; a carry never supersedes and is refused while one is open.
  Every append has an idempotency key stored per (principal, repository, verb) with its canonical payload: a repeat returns
  the first result, the same key with another payload is `key_reuse`. `replay` applies a recorded log by the same rules.
  A conflicting part whose request has an adopted result (parents exactly the prefix and the part head, no protected path
  touched) is merged by that commit and listed in the manifest as `resolution {part, submission, commit}` instead of
  being left out; `verify_manifest_with` requires the adopted result of the freeze's build and a replay that conflicts.

`plangraph land-train build` reads main for the repository of the `--repo` checkout and builds only when the tip it read
is the `--base`; it writes the build record (`<name>.open.json`: policy version, base, `--grant-digest`,
`--snapshot-position`) before the first merge, verifies manifest and result, and publishes the branch only after that.
Adopted resolutions come from the replay of `--resolution-log`, bound to the build and to the part's position in the
snapshot order. `plangraph land-train verify` repeats the proof with `--manifest`, `--parts` (the selection the verifier
recomputed), `--open`, `--freeze` and `--result`.

`verify --delta-out FILE` exports a replay artifact with the frozen base, train head, policy and each merge's
parents, actual tree and replay tree. For an adopted resolution it includes the diff from Git's conflict
tree to the committed tree. Each standalone resolution carries its commit and first parent, patch,
and Writer and Harness attribution. `land-train review` takes the same proof inputs and `--item KEY`. It dispatches
the canonical artifact as a witnessed document review from a clean checkout of the train head.
Only authors of the train delta are excluded. Replay and writer attribution read the same pinned
Git objects with replacement refs disabled. A mechanical merge can have an empty exclusion set.
`land-train admit --item KEY --review-run ID` repeats the proof and
requires a passing gateway-witnessed review in the keeper mirror. The serving attempt must be from
outside the delta's writer families, and the run must match the exact train head and artifact prompt.

## Deploy table and computed GO

`plangraph land-train deploy-plan --repo DIR --manifest FILE --authorized-hosts A,B --probe-cmd CMD
[--declared FILE] [--blockers-out FILE]` derives what each part ships from the part's own commits and
diff: a unit added or changed under `deploy/systemd`, or an added host binary, is a family of that part
alone, so a unitless part never waits on another part's unit. Hosts come from the `Deploy-Hosts:` of the
part's items (`--declared`, item key to hosts), else the `Deploy:` trailers of its commits, and each must be
authorized. `CMD HOST FAMILY` is the host install dry run: exit 0 with no output passes, and it prints one
`MISSING binary|file|env|path NAME` line per absent thing. The command prints `deploy: unit@host, ...` for
the GO-REQ (only passing targets) and one `blocker` line per refused target; `--blockers-out` writes a
`pg define` file with a blocker item for each.

`plangraph land-train go-verify --evidence FILE` checks the evidence of a GO from protected sources
(repository ref, workflow run jobs, approvals, delta review, replay, main state). It is pinned to this
binary's `tests.yml` digest and job matrix, binds repository, pull request, base, head, tree, manifest,
run and attempt, and lists every refusal: `moved_head`, `stale_base`, `replay_mismatch`, `job_missing`,
`job_not_green`, `mixed_attempt`, `stale_attempt`, `workflow_unpinned`, `ci_unbound`, `approval_missing`,
`delta_missing`, `operator_path_unauthorized` (a gate path without a `Gate-Edit` grant for this head) and
`main_not_green`.

## Push a built train

The builder defaults to `land/train-NAME` and saves the ordered part heads in
Git's common directory. Use `plangraph land-train push --repo DIR --branch
land/train-NAME` to push to origin with the installed hook, the manifest and
fast preflight. The hook uses the installed `plangraph land-train check-chain` command on `PATH`.
Push and replay use the same chain validator. It allows single-parent `Train N: resolve` and
`Train N: drop` commits with `Writer` and `Harness` trailers. The ordered
second parents must still match the manifest, including merges later reverted
by a drop. Use `Dropped-Part: <key>` and the canonical revert body to record
a dropped part.

Run a hand-started train resolver from its worktree with
`pg-spawnd hand-run -- PROGRAM [ARGS ...]`. Export `CARGO_TARGET_DIR` with the
host's configured shared target before launching it. The launcher validates
that setting before it starts the writer.

Use `--review` for a foreground review. For a detached user unit, add
`--unit NAME --result PATH` before `--`; review units also pass
`--review-output PATH`. Hand-run records the harness outcome in the host
lifecycle journal. The unit result records the child exit code and reports
a stuck-work alert for a failed child or empty review.

## A red train

Changes to shared test execution or setup need a successful full Tests workflow on their
own head before `merge_batch.py build` admits them. The part table records the run ID.
If a runner-wide part causes a train CI failure, drop it for its next train after its own
CI passes, unless a single commit fixes it. Track pre-existing races as fixes on main.

`land::split` (rust/plangraph-cli/src/land/split.rs) plans the retries of a red train; every retry is a new frozen
candidate (rule 3.52 (8), D-6535).

- The failing test paths name the part to leave out first: it is evicted with the job log, the parts above it wait
  (they are not blamed), the rest freezes as the next candidate.
- Without a hint, independent stacks are split into two halves at the stack boundary that balances them best (halves may
  be uneven). One failing stack is cut at a dependency-closed prefix; its descendants are blocked until the prefix landed and
  its exact head is verified in main, then the remainder is rebuilt against the new main.
- Two halves that both pass alone: the first lands, the second is rebuilt, reviewed and runs full CI again against the new
  main; a red second run leaves it unmerged.
- An evicted part goes back to its own writer with the job log; a part evicted twice goes to routing.
- Train size: 4 parts at the start, plus one after a green train, halved after a red one, between 1 and 8 (used once
  bounded trains are authorized by the amendment of rules 3.39 and 3.63).

## Stage timeline and bottleneck report

The keeper database holds the timeline: `landing_stages` (every attempt of fast checks, push, CI, review, delta review, GO,
merge and host sync per batch or train, with the host it ran on, its result and, for the push gate, the preflight family as
`detail`) and `landing_ci_jobs` (one row per CI job of a run from the GitHub API). Producers: the pre-push hook and
preflight (`tools/pgtest.py` `deliver_landing_stage`: one `fast_checks` row for the gate and one per family, with result),
the lander (`push`, `go`, `merge`, `host_sync`, written with `ok` or `failed`), the CI importer (host `github`, the run's
conclusion) and `plangraph review` (`review`, `delta_review`). `plangraph landing report OWNER/REPO --last N`
(rust/plangraph-cli/src/land/timeline.rs, landing_report.rs) reads the newest N landed trains through the keeper's `land`
actions `recent` (ordered by the time the merge stage ended, not by pull request number) and `timeline` and prints, per
train, the wall time from writer done to merge (it ends where the merge stage ends; the host sync is reported apart as the
time after it), the critical path (parallel jobs count once, the CI jobs replace the whole-run row), idle time between stages
and the bottleneck stage. A train is `incomplete` when it has no row for `fast_checks`, `push`, `ci`, `review`, `go`, `merge`
or `host_sync`, or none for a push-gate family (`push_gate_stages`); a landed train with no telemetry stays in the report as
incomplete. The last line names the stage with the most critical-path time over the N trains. `landing report OWNER/REPO
BATCH` and `--day DATE` keep their meaning. The GO request and train card print the same timeline.

## Settlement after the merge

`plangraph land-train settle --state FILE --repo-dir DIR [--init FILE]` (rust/plangraph-cli/src/land/settle.rs) runs the
steps after a train merges from a state file that holds the landed manifest (train, merge commit, parts with items, writer
and folded flag). The state is written before the first effect and after every result, so a crash resumes at the step it
stopped in; a finished step never runs again. Order: close each part's items (a folded part through the train merge),
verify the host sync (three distinct hosts at the merge commit), close the folded pull requests, stop the writers, then fold
the documentation fragments once. Refusals are `held` and checked again on the next run: an item stays open when its pull
request moved after the freeze or its writer has commits outside the landed tree; a pull request is read, closed and read
again, and reopened and left open when a push arrived in between; a writer is stopped only when its head is in the landed
tree and its pull request is closed. Ownership, READY time, CI, eviction and host-sync facts are recorded as events.

Production runs this path: `tools/merge_batch_land.py` (keeper journal) journals the landed parts first, `included_pr` closes
nothing there, and the journal step `close_parts` calls `land-train settle` with the landed parts as the manifest
(`merge_batch_build.run_settlement`, state in `merge-batch-settle/` beside the batch states). The step passes only when the
settlement is complete; otherwise it fails with the held steps and the journal stays pending, which is the hold on new
batches (rule 3.64): `plangraph landing journal` lists the train as pending until the settlement finished, and the lander
prints `HOLD-RELEASED` when it ends. Without the keeper journal the lander still closes the folded pull requests itself.
Each part's items are closed by `plangraph merge-close --item KEY...`, which the keeper scopes to the named items; a part is
done only when every item has a `closed` row or a `settled ... done` row. A `listed`, `deployment-pending`, `would-close`,
`merged-part` row, an item in another state, an item with no row, or a scan error holds the part.

## Documentation fragments

A change writes its documentation as `docs/<area>/<ITEM>.md` (areas `contributing`, `keeper`, `spawner`, which the docs-map
checks accept for CONTRIBUTING.md, docs/KEEPER.md and docs/SPAWNER.md), so two branches never edit the same reference.
`plangraph land-train fold-docs --repo-dir DIR` (rust/plangraph-cli/src/land/docs_fold.rs) runs once per train on main: it
appends each fragment, by area and name, as a `<!-- fragment:ITEM -->` block at the end of its reference, splits folded
lines over 600 characters at sentence ends, and removes only the fragments whose block is in the reference. Existing
reference text is never rewritten and a repeated run changes nothing. A fragment that would take its reference past the line
cap of tools/check_file_size.py (cap minus headroom) stays unfolded and is listed. `--check 1` prints what it would fold.
`--measure BASE..HEAD` counts the commits of a range that edit a shared reference directly; after the fold it should be
the one fold commit per train.

The settlement runs the fold as its last step (`fold_landed`): in a throwaway worktree of the landed merge commit it folds the
fragments and commits the result on the local branch `land/docs-fold-<train>`, whose parent is the merge; the checkout is
never touched and a repeat finds the branch. The branch is an ordinary commit for the next train to carry; its fragments that
did not fit a reference stay and are named in the settlement's `docs_fold` event. Publishing that branch is not automated:
it needs a lane writer identity the settlement does not have.
