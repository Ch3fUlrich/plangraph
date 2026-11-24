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

`plangraph land-timeline record|report` (rust/plangraph-cli/src/land/timeline.rs) keeps one append-only row per stage of a
batch or train (fast checks, push gate per stage, CI per job from the GitHub job list, review, GO request, merge, host sync):
subject, head, host, start, end, result. `report --last N` prints, per train, the wall time from writer done to merge, the
critical path (parallel jobs count once), idle time between stages and the bottleneck stage, then the stage with the most
critical-path time over the last N trains. The rows live in a JSON-lines ledger under the plangraph state directory; moving
them into a keeper table needs a keeper schema step that is not part of this change.

## Cutting a slice or landing branch

`plangraph land-train cut --repo DIR --base REV --branch NAME --commits A,B [--gate-edits A=D-1] --grants FILE`
(rust/plangraph-cli/src/land/cut.rs) resolves every input once to a full commit id, picks the sources (oldest first) onto
`--base` without `-x`, drops any `(cherry picked from commit ...)` line and adds `Picked-From: <full id>` and, for a commit named
in `--gate-edits`, `Gate-Edit: <D-id>` inside the existing trailer block. Then it runs `tools/ci_commit_gate.py check` on the
range with full ids. The gate is the one of the base commit, as CI reads it, and runs under the cutter's git environment (no
`GIT_*` override, no replacement refs); no flag names another gate. The branch is created by one atomic ref update that fails
when the branch exists, and that update is the success boundary: a scratch worktree that cannot be removed afterwards is a
warning line (`git worktree prune` after deleting its directory under `<git-common-dir>/plangraph/cut-worktrees/`), never a failed cut.
A merge commit as a source is refused.

`--grants FILE` is the pre-cut grant record, a JSON array of `{d_id, prefix, head}` (the `go_verify` grant). It is operator
authority, so only a protected file is read: a regular file (no link) owned by the caller and not writable by group or others
(mode 0600). A `Gate-Edit` D-id, whether named in `--gate-edits` or already carried by a source anywhere in its message (also
where git's trailer parser misses it), needs a row for that D-id and the source head, with prefixes covering every path the
commit changes (read NUL-delimited; the pinned gate decides which paths are gate files). The commit must be single-parent. A hand-cut commit must also
reproduce its source: the `Picked-From:` commit has to be in the repository with the same patch id. The record is written by the
router before the cut, because the GO row binds a head that exists only after the cut; its producer is a follow-up item, so
until it lands the file is written by hand. `land-train cut-check --repo DIR --base REV --head REV [--grants FILE]` runs the same message check and the same gate on a
branch cut by hand (the source head is its last `Picked-From:`) and refuses a `cherry-pick -x` line.
