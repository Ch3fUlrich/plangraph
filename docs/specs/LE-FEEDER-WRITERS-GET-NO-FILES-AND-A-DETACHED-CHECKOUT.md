# LE-FEEDER-WRITERS-GET-NO-FILES-AND-A-DETACHED-CHECKOUT

Outcome. The feeder claims only items whose spec card on main names a write set, gives the writer exactly those files and its lane branch, and releases every claim of a run that ends without a commit.

- plan_source. Keeper item plangraph:LE-FEEDER-WRITERS-GET-NO-FILES-AND-A-DETACHED-CHECKOUT, priority 10, D-7041, D-7042, D-7044, D-7047; incident 2026-11-27 10:21Z to 10:55Z (34 feeder runs `no_commit` in about 1 minute each, Files section empty, 175 claims held by the feeder principal, feeders paused at 10:55Z); rule 3.118 (D-7083).
- base commit. 443bf493af86a2ccd1d2859fb4e1d52eceea522d
- card SHA-256. 5f3a33f802d374db361c29547f80eb6bae1cddb900e17c3c5c37173c64dfac74
- tree SHA-256. 7e57e9ce71998ca7e7707cd9cc0b473778f6ab6a7206dd2e2efc598e35e18f4f
- evidence_after_deploy. After `feeder.off` is removed on coding.vm, dell and the workstation: 10 consecutive feeder runs per host each end with at least one commit or with their claims released (keeper audit `release` rows by the feeder principal within one tick of the run's end); no run brief has an empty `Files:` section; the dry-run decision lists `no_spec` skips for ready items without a card. Record run ids, hosts and the keeper audit range.

## Checked against base

- Premise re-confirmed at 443bf493a (stale markers D-7044, D-7047): `Task.files` comes only from the keeper item's `files` field or a `files:` note label; the brief prints `Files:` from it and the card carries `files`; nothing reads `docs/specs/`. [rust/plangraph-cli/src/feeder/mod.rs:23-41,208-243; rust/plangraph-cli/src/feeder/selection.rs:4-64]
- The batch card already names the lane: `push_ref = refs/heads/lane/feeder-<first item slug>`; the native host pins the base and works on a detached worktree by design and pushes the accepted head to `push_ref`. The brief never tells the writer this, so the writer asks for an attached branch. [rust/plangraph-cli/src/feeder/workflow.rs:150-210; rust/pg-spawnd/src/native_writer_host.rs:303-342,448-455]
- Claims: `claim_batch` (workflow) claims each member and releases on drop or spawn failure; after a successful spawn the launch receipt (`launch-*/receipt.json`: `run_id`, `item_key`, `item_keys`, `files`) is the feeder's only memory of the run, and `associations` deletes it once the run is terminal, without releasing anything. The receipt has no epochs; `Run` has no `reason`. [rust/plangraph-cli/src/feeder/workflow.rs:29-110; rust/plangraph-cli/src/feeder/adapters.rs:211-240,340-365; rust/plangraph-cli/src/feeder/sources.rs:40-97]
- Spec card format: JSON in `docs/specs/<ID>.card.yaml`, `cards.<ID>.write_set` a list of relative paths (docs/PLANNER-KIT.md "Card YAML").
- The run's base is `refs/heads/main` of the feeder's `--repo` checkout (`card.base_ref`). [rust/plangraph-cli/src/feeder/mod.rs:239-243]

## Decisions

- D1. Spec source. New module `feeder/spec.rs` with `trait SpecSource { fn write_set(&mut self, key: &str) -> SpecRead }` and the live `GitSpecs { repo }`: `git -C <repo> show refs/heads/main:docs/specs/<ID>.card.yaml` (ID = key after `project:`, exact case, ASCII `[A-Za-z0-9][A-Za-z0-9._-]{0,127}`), bounded 5 s through `host_command`. It reads the same ref the writer's base comes from, so an admitted item's spec is in the writer's base. `SpecRead` is `Files(Vec<String>)`, `Missing`, `Invalid(reason)`, `Unreadable(reason)`.
- D2. Admission. Before any claim, `tick` asks the spec source for each candidate: `Missing` skips the item with reason `no_spec`, `Invalid` with `spec_invalid: <reason>` (not JSON, no card for the ID, empty `write_set`, an absolute, `~` or `..` path), `Unreadable` with `spec_unreadable: <reason>`. A skipped item is never claimed and the tick moves on to the next item. For an admitted item `Task.files` is replaced by the card's `write_set` (keeper `files` or note labels no longer decide); overlap and batch union use these files. Dry runs apply the same reads.
- D3. Lane branch in the brief. The single-item brief and the batch input name the lane: `Lane branch: lane/<run name>. The worktree is detached at the pinned base on purpose: commit on it; the driver pushes your accepted head to this branch. Do not create or switch branches.` (single brief: a line after `Files:`; batch input: `run.lane_branch`). The card's `push_ref` is unchanged and names the same branch.
- D4. Release on no commit. The spawn card carries `claims: [{key, epoch}]` for every held member and the receipt copies it. `Run` gains `reason`. On a live tick, for each receipt whose run is terminal and whose `reason` starts with `no_commit`, the feeder releases every listed claim (`release?fmt=json {key, epoch}`) before deleting the receipt; a release refused as `epoch_stale`/`bad_state` counts as done (someone else holds it now), a transport failure keeps the receipt for the next tick. Terminal runs with other reasons keep today's behavior (claims stay for the orchestrator). Dry ticks report `would_release` and delete nothing.

## Entry points

- E1. `plangraph feed --once [--dry-run]` tick: admission and brief/card.
- E2. The same tick's receipt pass: release of a `no_commit` run's claims.

## Acceptance

| Row | Entry | Observable result | Evidence |
| --- | --- | --- | --- |
| B1 | E1 | A ready item without a spec card is skipped `no_spec` and never claimed; an invalid card is skipped `spec_invalid`; the next item with a card is claimed in the same tick. | X1 |
| B2 | E1 | An item with a card gets a run card whose `files` equal the card's `write_set`, a `push_ref` on `lane/feeder-...`, and a brief whose `Files:` lists those paths and whose lane line names the same branch; a batch gets the union. | X2 |
| B3 | E2 | A run that ends `no_commit` has every claim it holds released within the next live tick, then its receipt is gone; a run ending otherwise keeps its claims. | X3 |
| B4 | E2 | A transport failure on release keeps the receipt; a dry tick releases nothing and lists `would_release`. | X3 |

## Verification

- X1. `rust/plangraph-cli/tests/feeder.rs::a_ready_item_without_a_spec_card_is_skipped_as_no_spec_and_not_claimed`.
- X2. `tests/feeder.rs::an_item_with_a_spec_card_gets_its_write_set_and_a_lane_branch`.
- X3. `tests/feeder.rs::a_no_commit_run_releases_every_claim_it_holds` and `::a_failed_release_keeps_the_receipt_and_a_dry_tick_only_reports`.

Write X1 to X3 first; on the base X1 claims the item, X2 has an empty file list and no lane line, X3 releases nothing. The fixture repo in `tests/feeder_support/mod.rs` gains a `refs/heads/main` with spec cards; the existing feeder suites give their fixture items a card by default, so their assertions stay as they are.

## Scope and reuse

- R1. Reuse `host_command`, the keeper `release` call of `Adapters`, `claim_batch`, the receipt file, the card JSON format. No YAML parser: spec cards are JSON.
- N1. Out of scope: keeper-side spec admission (LE-ITEMS-ACCEPTED-AND-CLAIMED-WITHOUT-A-SPEC), attaching the native worktree to a branch (the detached base is the native writer's design), fast checks in other repositories (LE-FAST-CHECKS-FAIL-IN-EVERY-NON-PLANGRAPH-REPOSITORY).

## Lane scope outside the spec

Lane lane/feeder-spec-files-d7044 (head f11e3ef67) implements this item together with LE-FAST-CHECKS-FAIL-IN-EVERY-NON-PLANGRAPH-REPOSITORY; its stat lists 14 of 15 changed files. Outside this write_set:

- deploy/feeder/policy.toml: D1 to D4 need no policy setting; justify a flag or drop it.
- rust/plangraph-cli/src/fast_checks.rs: belongs to LE-FAST-CHECKS-FAIL-IN-EVERY-NON-PLANGRAPH-REPOSITORY, not this item.

Not in the lane: rust/plangraph-cli/src/feeder/adapters.rs (D4: the receipt must carry the claim epochs) and rust/plangraph-cli/src/feeder/selection.rs (D2: skipped rows join the existing `skipped` list); the reviewer asks how the lane releases claims without epochs.

## Open points for design approval

None.
