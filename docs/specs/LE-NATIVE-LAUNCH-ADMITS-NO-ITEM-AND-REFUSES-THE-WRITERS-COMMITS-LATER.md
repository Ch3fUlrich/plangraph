# LE-NATIVE-LAUNCH-ADMITS-NO-ITEM-AND-REFUSES-THE-WRITERS-COMMITS-LATER

Outcome. A native Codex launch whose brief names item keys the writer could not commit, or that admits no key at all, is refused at lane admission with the `Plan item:` hint. The card's own keys are always admitted for the writer's `Plan-Item` trailers, and a second live run on the same `review_dir` is refused.

- plan_source. Keeper item plangraph:LE-NATIVE-LAUNCH-ADMITS-NO-ITEM-AND-REFUSES-THE-WRITERS-COMMITS-LATER, 2026-11-27 16:48Z, priority 10.
- base commit. 443bf493af86a2ccd1d2859fb4e1d52eceea522d
- card SHA-256. 0766bb5b44d42de2ed4bb3319da2d69fe5d033a907bf105adb981637a30b8ac3
- tree SHA-256. dddf924736c39aa2af1977b4a20a03491bddc00f556b09a891761d0449fee8a2
- evidence_after_deploy. On the workstation guest (one host first, rule 3.112 (1)), launch one feeder native card whose brief uses `Item: <key>` and record the `no_admitted_item` refusal. Launch the same card with `Plan item: <key>` lines and record its run reaching review and push. Then, 7 days after deployment, check `grep -l 'Plan-Item outside writer brief' ~/.local/state/plangraph/runs/*/record.json` on every host and expect no record newer than the deployment.

## Checked against base

- The native writer's commit gate allows only `Plan-Item` trailers whose key is in `context::item_keys(&self.brief)`. That is `lane_guard::item_keys`, which reads one statement form per line: `## key`, `### key`, `Plan item: key` or `Item key: key`. `Item: <key>` and keys listed in one prose line are not read, and neither are the card's `item_key` and `item_keys`. [rust/pg-spawnd/src/native_writer_review.rs:169–196; rust/pg-spawnd/src/native_writer_context.rs:65–67; rust/pg-spawnd/src/lane_guard.rs:113–145]
- Lane admission (`pg-spawnd lane-admit`, the one door of every native launch, called by `overflow.lane_admit` before the tool and sandbox probes) admits the brief's statement keys plus the card keys (`card_keys`: `item_key`, else `item`, else `PLANGRAPH_SPAWN_ITEM`, plus `item_keys`). It writes them into the private request card as `item_keys` with `lane_admitted: true`. A launch that admits no key passes. [rust/pg-spawnd/src/lane_admit.rs:65–121; rust/pg-spawnd/src/lane_guard.rs:89–111,343–366; src/plangraph/spawner/overflow.py:556–569,596–608]
- So the four 16:0x–17:3xZ workstation runs (run-fe6171d3, run-6af94fa6, run-f65c9e18 and run-4ed9e71d) were admitted, did their work and were refused only at the commit gate. Their briefs said `Item: <key>`, and the card keys never reached `Host::brief`. `Host::configure` sets the brief from the rendered prompt only. [rust/pg-spawnd/src/native_writer_commands.rs:33–89]
- `review_dir` comes from the card, or defaults to `<cwd>/reviews`. Nothing checks it against other live runs, and the four runs shared one `review_dir` and its `status.txt`. [rust/pg-spawnd/src/native_writer_commands.rs:44–48; rust/pg-spawnd/src/native_writer_host.rs:482–493]
- Lane refusals keep a stable reason through `lane_admit::REASONS` (MCP error data, placement) and Python `run_refusal._LANE_REFUSALS`. A remote refusal outside that list is reported only as `target rejected admission`. [rust/pg-spawnd/src/lane_admit.rs:16–25; rust/pg-spawnd/src/errors.rs:98–101; src/plangraph/spawner/run_refusal.py:77–92]
- Two existing native-spawn tests launch with `prompt='unused'` and a keyless card to reach the tool and sandbox probes that come after admission. [rust/pg-spawnd/tests/native_tools.rs:52,138]

## Decisions

- D1. Card keys admitted for commits (item step 2). `Host::configure`, after rendering the brief, appends one line `Plan item: <key>` for each key of the request card that the rendered brief's `item_keys` lacks. The keys are `lane_admit::card_keys(card, None)` (the singular `item_key`, else `item`, then `item_keys`, validated as admission validates them), normalized with `lane_guard::normalize`, in card order. The private request card is written only by `overflow.native_spawn` after `lane-admit`, so these are exactly the admitted keys. The writer prompt, the reviewer context and the trailer gate then share one key set. Fresh launches and `writer-resume` both go through `execute_request` → `configure`.
- D2. Launch refusal (item step 1). In `admit_lane`, after `admit_launch` returns the admitted keys, the launch is refused as `no_admitted_item` in two cases. (a) The admitted set is empty: `no_admitted_item: the brief and card name no item key; use 'Plan item: <key>' lines`. (b) The brief, unless it is a typed batch input (`batch_writer_brief::typed_keys` is `Some`), has item label lines that name a valid key outside the admitted set: `no_admitted_item: use 'Plan item: <key>' lines; not admitted: <k1>, <k2>`. A label line is a line that, after trimming leading spaces and an optional `- ` or `* ` bullet, starts case-insensitively with `item:`, `items:`, `item keys:`, `plan items:`, `plan-item:`, `key:` or `keys:`. Its keys are the tokens after the label, split on whitespace, `,`, `;` and `|`, stripped of surrounding backticks, quotes and one trailing `.`, that pass `valid_key`, then normalized. The item's "fewer keys than the card names" case is covered by D1: card keys are always admitted, so a brief that names fewer keys strands no work.
- D3. Review directory (item step 3). In `admit_lane`, after D2, when the card has a string `review_dir`, the launch is refused with `review_dir_in_use: <dir> is used by live run <name>` if a run record that `lane_guard::scan_runs` reports as holding (live implement) has an equal `review_dir`. Two paths are equal when both canonicalize to the same path, or else when their lexical components are equal. A card without `review_dir` gets the per-worktree default and is not checked.
- D4. Stable reasons. `lane_admit::REASONS` becomes `["lane_unreadable", "lane_collision", "lane_too_many_keys", "no_admitted_item", "review_dir_in_use"]`, and Python `_LANE_REFUSALS` gains `'no_admitted_item:'` and `'review_dir_in_use:'`. Remote launches then report the hint text instead of `target rejected admission`.
- D5. Code placement. The three helpers (`unadmitted_brief_keys`, `with_card_keys`, `refuse_review_dir`) go in a new bin module `rust/pg-spawnd/src/launch_admission.rs` that uses the public `pg_spawnd::lane_guard` functions. `lane_guard.rs` and its tests stay unchanged; LE-SPAWND-ONE-OVERSIZE-RUN-RECORD-BLOCKS-THE-LANE edits those files.
- D6. Docs (item step 4). `docs/native-codex-writer.md` gets a "Brief item lines" paragraph naming the four accepted forms, the D1 rule and both refusals. The spawner fragment states D4.

## Entry points

- E1. Native launch admission: `pg-spawnd lane-admit` (from `overflow.lane_admit`, used by `native_spawn` and `native_queue_card`) → `lane_admit::admit_lane`.
- E2. Native writer run: `native_writer_commands::execute_request` → `Host::configure` → the brief used by the writer prompt, the reviewer and `check_item_trailers`.

## Acceptance

| Row | Entry | Observable result | Evidence |
| --- | --- | --- | --- |
| B1 | E1 | A launch with prompt `Item: demo:K\n` and card `{}` is refused. The message starts `no_admitted_item:`, contains `Plan item:` and `demo:K`, and no run record is created. | X1 X3 |
| B2 | E1 | A launch with prompt `Do the work\n` and card `{"item_keys":["demo:A","demo:B"]}` is admitted with `item_keys` `["demo:A","demo:B"]`. A launch with prompt `Do the work\n` and card `{}` is refused `no_admitted_item`. | X1 X3 |
| B3 | E1 | With a live implement record whose `review_dir` is `D`, a launch with card `review_dir` `D` and a free key is refused `review_dir_in_use`, naming the run. The same launch with another directory is admitted. | X3 |
| B4 | E2 | After `configure` with card `{"item_key":"plangraph:A","item_keys":["plangraph:A","plangraph:B"]}` and prompt `fixture`, `context::item_keys(&host.brief)` is `["plangraph:A","plangraph:B"]`. A second `configure` adds no duplicate line. | X2 |
| S1 | E1 E2 | For 7 days after deployment, no native run on any host ends with `Plan-Item outside writer brief`. | X4 |

## Verification

- X1. E1, changed. `rust/pg-spawnd/src/launch_admission_tests.rs`: `label_lines_report_keys_outside_the_admitted_set` (`Item:`, `Items: a, b`, a bulleted `- Plan-Item:` line, a key in backticks, case variants; `Plan item:` and `### key` lines report nothing; a typed batch input reports nothing), `card_keys_become_plan_item_lines_once` and `review_dirs_compare_canonical_then_lexical`. Check: `cargo test --manifest-path rust/Cargo.toml --offline --locked -p pg-spawnd --bin pg-spawnd launch_admission`.
- X2. E2, changed. `rust/pg-spawnd/src/native_writer_host_review_tests.rs::configure_admits_the_card_keys_the_brief_omits`, built like `a_fresh_feeder_launch_renders_the_gate_paths_before_its_worktree_exists`. Check: `cargo test --manifest-path rust/Cargo.toml --offline --locked -p pg-spawnd --bin pg-spawnd native_writer_host::review_tests`. The existing `assert_item_trailers_bound_to_brief` keeps proving that an unassigned trailer is still refused.
- X3. E1, changed. `rust/pg-spawnd/tests/lane_admit.rs`: `a_brief_naming_its_item_as_item_colon_is_refused_with_the_hint`, `a_card_with_two_item_keys_admits_both_without_brief_lines`, `a_launch_admitting_no_key_is_refused` and `a_second_live_run_on_the_same_review_dir_is_refused`, using the file's `lane_admit` and `write_run` helpers. In `rust/pg-spawnd/tests/native_tools.rs`, the two `prompt='unused'` launches become `prompt='Plan item: demo:TOOLS\n'` so they still reach their tool and sandbox refusals. Check: `cargo test --manifest-path rust/Cargo.toml --offline --locked -p pg-spawnd --test lane_admit --test native_tools`.
- X4. Live only. The evidence_after_deploy observation.
- X5. Held out. `cargo test --manifest-path rust/Cargo.toml --offline --locked -p pg-spawnd --lib lane_guard::tests && cargo test --manifest-path rust/Cargo.toml --offline --locked -p pg-spawnd --test native_writer --test native_evidence`, unchanged and green.

Write X1–X3 first and see B1–B4 fail on the base.

## Scope and reuse

- R1. Reuse `lane_guard::{item_keys, valid_key, normalize, scan_runs}`, `batch_writer_brief::typed_keys`, `lane_admit::card_keys` and the existing refusal plumbing. The trailer gate keeps its single source, `Host::brief`.
- N1. Steering (LE-NATIVE-WRITER-BRIEF-KEYS-FIXED-AT-LAUNCH-STEER-CANNOT-ADMIT), the plain Codex spawn path (LE-SPAWNED-CODEX-WRITER-CANNOT-COMMIT-OR-BUILD-IN-A-LANE-WORKTREE) and the feeder's brief template are not changed. Feeder briefs that use `Item:` are refused with the hint until the template uses `Plan item:`. That is the intended signal.

### File budgets

`lane_admit.rs` 121 → at most 140 lines. `native_writer_commands.rs` 398 → at most 402 lines (one call in `configure`). `main.rs` gets one `mod launch_admission;` line. The new module and its tests stay within 120 and 110 lines. `native_writer_host_review_tests.rs` 449 → at most 479 lines. `tests/lane_admit.rs` 470 → at most 560 lines. `run_refusal.py` changes one line. `docs/native-codex-writer.md` 258 → at most 270 lines.

### Implementation

- `launch_admission.rs`: `pub(crate) fn unadmitted_brief_keys(prompt: &str, admitted: &[String]) -> Vec<String>` (D2 b, sorted and unique). `pub(crate) fn with_card_keys(brief: &str, card: &Value) -> String` (D1). `pub(crate) fn refuse_review_dir(state: &Path, review_dir: Option<&str>) -> Result<(), Refusal>` (D3, reusing `lane_guard::Refusal`). End the file with `#[cfg(test)] #[path = "launch_admission_tests.rs"] mod tests;`.
- `lane_admit.rs`: in `admit_lane`, after `admit_launch` succeeds, apply D2, then D3, mapping each refusal to `ToolError::Refused { kind: RefusalKind::Lane(code), .. }` as the existing lane refusals are mapped. Extend `REASONS` per D4.
- `native_writer_commands.rs`: in `configure`, `self.brief = crate::launch_admission::with_card_keys(&rendered, &request["card"]);`.
- `run_refusal.py`: D4.

## Open points for design approval

None.
