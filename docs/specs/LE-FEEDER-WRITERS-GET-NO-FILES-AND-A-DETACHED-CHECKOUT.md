# LE-FEEDER-WRITERS-GET-NO-FILES-AND-A-DETACHED-CHECKOUT

Outcome. The feeder claims only items whose spec card on origin's main names a write set, uses those files as the item's scope everywhere, gives the writer exactly those files and its lane branch, and releases every claim of a run that ended with no commit at all.

- plan_source. Keeper item plangraph:LE-FEEDER-WRITERS-GET-NO-FILES-AND-A-DETACHED-CHECKOUT, priority 10, D-7041, D-7042, D-7044, D-7047; incident 2026-11-27 10:21Z to 10:55Z (34 feeder runs `no_commit` in about 1 minute each, Files section empty, 175 claims held by the feeder principal, feeders paused at 10:55Z); rule 3.118 (D-7083).
- base commit. 443bf493af86a2ccd1d2859fb4e1d52eceea522d
- card SHA-256. 4581db24ad6cc7a4e4a858169a3eb0691d30336790d028649d8159684fa1d884
- tree SHA-256. 129ba3016dce8bf0aa8c526987c3b01f38b649903ca685bb38edcdd9d5272fca
- evidence_after_deploy. After `feeder.off` is removed on coding.vm, dell and the workstation: 10 consecutive feeder runs per host each end with at least one commit or with their claims released (keeper audit `release` rows by the feeder principal within one tick of the run's end); no run brief has an empty `Files:` section; the dry-run decision lists `no_spec` skips for ready items without a card. Record run ids, hosts and the keeper audit range.

## Checked against base

- Premise re-confirmed at 443bf493a (stale markers D-7044, D-7047): `Task.files` comes only from the keeper item's `files` field or a `files:` note label; the brief prints `Files:` from it and the card carries `files`; nothing reads `docs/specs/`. [rust/plangraph-cli/src/feeder/mod.rs:23-41,208-243; rust/plangraph-cli/src/feeder/selection.rs:4-64]
- Scope outside the candidate also comes from keeper fields: an active run's files are rebuilt from the keeper snapshot of its members, and `rival` compares a claimed item's keeper `files`. [rust/plangraph-cli/src/feeder/sources.rs:97-163; rust/plangraph-cli/src/feeder/adapters.rs:265-302]
- Spec cards: every checked-in `docs/specs/*.card.yaml` is JSON (for example docs/specs/FEEDER-WSL-RAM-AND-CPU-PRESSURE.card.yaml:1-28), `cards.<ID>.write_set` a list of relative paths; docs/PLANNER-KIT.md:21-51 also allows YAML forms for batch imports. The feeder requires the JSON form.
- The card's `base_ref` is `refs/heads/main`; the native writer fetches origin's main and pins origin's exact ref when origin has it, the host checkout's local branch only otherwise. Local and remote main of the feeder checkout can differ. [rust/plangraph-cli/src/feeder/mod.rs:239-243; rust/pg-spawnd/src/native_writer_base.rs:75-108; rust/pg-spawnd/src/native_writer_host.rs:307-326]
- The batch card already names the lane: `push_ref = refs/heads/lane/feeder-<first item slug>`; the native host works on a detached worktree by design and pushes the accepted head to `push_ref`. The brief never says so. The batch renderer refuses unknown `run` fields and copies each item's `brief` verbatim under its heading. [rust/plangraph-cli/src/feeder/workflow.rs:150-210; rust/pg-spawnd/src/native_writer_host.rs:330-342,448-455; rust/pg-spawnd/src/batch_writer_brief.rs:24-45,164-182]
- Outcome fields: `no_commit:` reasons cover both a writer that never committed (`... ended its turn without a new commit`) and a push repair after an accepted review (`push fix left HEAD at`); `review_rounds` starts at 0 and counts reviews, which need a commit. [rust/pg-spawnd/src/native_writer.rs:84-93,479-487,569-589]
- Claims: `claim_batch` claims each member and releases on drop or spawn failure. The spawner's run record keeps the launch card and `ps` lists local records in full (`row["card"]` is read by the feeder); the launch receipt is written after the spawner started and its write can fail with the run going on; `associations` deletes it once the run is terminal, releasing nothing. [rust/plangraph-cli/src/feeder/workflow.rs:29-110; rust/plangraph-cli/src/feeder/sources.rs:40-96; rust/plangraph-cli/src/feeder/adapters.rs:340-365; src/plangraph/spawner/service.py:235-239]

## Decisions

- D1. Spec commit. Once per tick the feeder runs `git -C <repo> fetch --no-write-fetch-head origin +refs/heads/main:refs/remotes/origin/main` (the native writer's own fetch form), resolves `refs/remotes/origin/main` to one commit S and reads every spec of the tick at S: `git show S:docs/specs/<ID>.card.yaml`, bounded 5 s through `host_command`. ID = key after `project:`, exact case, ASCII `[A-Za-z0-9][A-Za-z0-9._-]{0,127}`. The card records `spec_main: S`. A failed fetch refuses the tick (`spec_main_unavailable`). New module `feeder/spec.rs`: `trait SpecSource { fn write_set(&mut self, key: &str) -> SpecRead }`, `SpecRead` = `Files(Vec<String>)`, `Missing`, `Invalid(reason)`, `Unreadable(reason)`; the live `GitSpecs { repo, main: S }`.
- D2. Admission. Before any claim, a candidate with `Missing` is skipped `no_spec`, `Invalid` `spec_invalid: <reason>` (not JSON, no card for the ID, empty `write_set`, an absolute, `~` or `..` path), `Unreadable` `spec_unreadable: <reason>`; it is never claimed and the tick goes on. Skips join the existing `skipped` list. Dry runs read the same way.
- D3. One scope. `scope(key)` = the spec `write_set` at S, else the keeper `files` (items without a spec, e.g. hand lanes). It is used for candidates and batch unions, for active runs (the run's launch card `files` first, which is the admitted scope, then `scope` per member), and for `rival` (the other claimed item's `scope`; unreadable stays a rival). `Adapters` holds the same `SpecSource` for `rival`.
- D4. Lane line. `brief(item, rules, lane)` adds after `Files:` the line `Lane branch: lane/<run name>. The worktree is detached at the pinned base on purpose: commit on it; the driver pushes your accepted head to this branch. Do not create or switch branches.` A single run's brief and every item brief inside the batch input carry it; the batch `run` object is unchanged (the renderer refuses new fields and copies item briefs verbatim). The lane equals the card's `push_ref` branch.
- D5. Zero-commit outcome. A run ended with no commit at all when its record has `review_rounds == 0`, no `pushed`, and a `reason` starting with `no_commit: the writer ended its turn without a new commit`. A push-repair `no_commit` (after an accepted review, `review_rounds >= 1`) and every other ending keep their claims for the orchestrator. `Run` gains `reason`, `review_rounds`, `pushed`.
- D6. Release. The launch card carries `claims: [{key, epoch}]` for every held member. On a live tick, for each local `ps` row (host = this host) that is terminal with a zero-commit outcome, the feeder releases every claim of `row.card.claims` (`release?fmt=json {key, epoch}`) and then writes `released-<run_id>` in its state directory, so a restart neither repeats nor loses the pass. `epoch_stale`/`bad_state` refusals count as released; a transport failure leaves no marker and retries next tick. A card without `claims` (launched before this change) is logged once as `release_skipped: no claim epochs` and left to lease expiry. The receipt is no longer needed for release. Dry ticks list `would_release`.
- D7. Size. `sources.rs` (754 lines) grows by at most 36 net lines; `release` and `scope` live in `spec.rs`. Every file has a cap in the card's `line_caps`.

## Entry points

- E1. `plangraph feed --once [--dry-run]` tick: spec commit, admission, scope, brief and card.
- E2. The same tick's release pass over local terminal runs.

## Acceptance

| Row | Entry | Observable result | Evidence |
| --- | --- | --- | --- |
| B1 | E1 | A ready item without a spec card is skipped `no_spec` and never claimed; an invalid card is skipped `spec_invalid`; the next item with a card is claimed in the same tick. | X1 |
| B2 | E1 | With local main behind origin's main, the feeder reads the spec from origin's commit and records it as `spec_main`. | X2 |
| B3 | E1 | An item with a card gets a run card whose `files` equal the `write_set`, a `push_ref` on `lane/feeder-...`, and a brief whose `Files:` lists those paths and whose lane line names the `push_ref` branch, for a single run and for every item brief of a batch. | X3 |
| B4 | E1 | A candidate whose spec shares a file with another host's claimed item that has empty keeper `files` yields `files_reserved_by`. | X4 |
| B5 | E2 | A zero-commit run has every claim released within the next live tick, even when its receipt write failed and the feeder restarted; a push-repair `no_commit` run keeps its claims; a dry tick only reports. | X5 |

## Verification

- X1. `rust/plangraph-cli/tests/feeder.rs::a_ready_item_without_a_spec_card_is_skipped_as_no_spec_and_not_claimed`.
- X2. `tests/feeder.rs::the_spec_is_read_from_origin_main_when_local_main_differs`.
- X3. `tests/feeder.rs::an_item_with_a_spec_card_gets_its_write_set_and_a_lane_branch` (single and batch).
- X4. `tests/feeder.rs::a_claimed_item_with_empty_keeper_files_still_reserves_its_spec_files`.
- X5. `tests/feeder.rs::a_zero_commit_run_releases_every_claim_after_a_lost_receipt_and_a_restart` and `::a_push_repair_no_commit_keeps_its_claims`.

Write X1 to X5 first; on the base X1 claims the item, X2 to X4 see keeper scope only, X5 releases nothing. The fixture in `tests/feeder_support/mod.rs` gains a bare origin whose main carries spec cards; the existing feeder suites give their fixture items a card by default, so their assertions stay as they are.

## Scope and reuse

- R1. Reuse `host_command`, the keeper `release` call of `Adapters`, `claim_batch`, `ps` rows, the card JSON format. No YAML parser.
- N1. Out of scope: keeper-side spec admission (LE-ITEMS-ACCEPTED-AND-CLAIMED-WITHOUT-A-SPEC), attaching the native worktree to a branch, fast checks in other repositories (LE-FAST-CHECKS-FAIL-IN-EVERY-NON-PLANGRAPH-REPOSITORY).

## Review r1

- WRONG .md:16 and .md:17 fixed above; D2 (scope), D3 (lane line in item briefs), D4 (zero-commit outcome), D5 (release from the spawner's card, restart marker), D6 (line caps) applied.
- Review r1 D1: rejected in part, passing S to the native launch: the native card takes only a `refs/heads/` or `refs/tags/` base and pins origin's ref at launch (rust/pg-spawnd/src/native_writer_host.rs:307-326, rust/pg-spawnd/src/native_writer_base.rs:75-108); origin's main only moves forward, so S is an ancestor of the writer's base, and the writer takes its files from the card, not from the base. Reading at origin's main (D1) removes the local/remote divergence the point names.

## Lane scope outside the spec

Lane lane/feeder-spec-files-d7044 (head f11e3ef67) implements this item together with LE-FAST-CHECKS-FAIL-IN-EVERY-NON-PLANGRAPH-REPOSITORY; its stat lists 14 of 15 changed files. Outside this write_set:

- deploy/feeder/policy.toml: D1 to D7 need no policy setting; justify a flag or drop it.
- rust/plangraph-cli/src/fast_checks.rs: belongs to LE-FAST-CHECKS-FAIL-IN-EVERY-NON-PLANGRAPH-REPOSITORY, not this item.

Not in the lane: rust/plangraph-cli/src/feeder/adapters.rs (D3 rival scope, D6 claims in the card) and rust/plangraph-cli/src/feeder/selection.rs (D2 skips).

## Open points for design approval

None.
