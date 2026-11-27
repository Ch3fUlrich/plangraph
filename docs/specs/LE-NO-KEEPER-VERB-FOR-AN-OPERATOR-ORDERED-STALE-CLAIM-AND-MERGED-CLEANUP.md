# LE-NO-KEEPER-VERB-FOR-AN-OPERATOR-ORDERED-STALE-CLAIM-AND-MERGED-CLEANUP

Outcome. A release-review verb returns stale in_review claims to open under a decision id after a preview, and close-merged on the operator waiver row accepts a named commit on the default branch (and a PR number) where no Plan-Item trailer commit exists, both over a list file.

- plan_source. Keeper item plangraph:LE-NO-KEEPER-VERB-FOR-AN-OPERATOR-ORDERED-STALE-CLAIM-AND-MERGED-CLEANUP, operator order 2026-11-27 12:01Z, priority 10, D-7046, D-7048, D-7060, D-7079; incidents 14:18Z and 15:15Z.
- base commit. 443bf493af86a2ccd1d2859fb4e1d52eceea522d
- card SHA-256. 66d42757acc279768f0632d7eaf9f599fb3c06d759bd8d8cf0e003b963b3753a
- tree SHA-256. 9bbbc28035c733c19140ba504a3a3fb9edfcd7dce3cdf6212eb70e67fc424b71
- evidence_after_deploy. After the keeper restart: run `release-review --file the plan-roadmap state file of 2026-11-27 12:01Z (stale claims) --decision-ref D-N` (preview) and then with `--apply`; run `close-merged --file <remaining merged list> D-W --waive-seats D-W` (preview, then `--apply`) on a new OPERATOR `Waiver: merged-without-witnesses` row whose `Waives-seats:` lists the remaining keys and whose `Cutoff:` covers their merges. Record both TSV outputs (counts per class) in the item evidence. Writing the waiver row is routing's step with the operator's D-id.

## Checked against base

- `release` takes only `claimed`; `reap` expires only `claimed`. [src/plangraph/keeper/store_verbs.py:409-449; src/plangraph/keeper/store_reap.py:36-45]
- `_release_claim(principal, item, payload, now)` already writes a `released` event and moves any `(state, epoch)` row to `open`, clearing claim, delegate, reviewer, lease and waiver. [src/plangraph/keeper/store_verbs.py:440-449]
- Batch waiver already exists: an OPERATOR row `Waiver: merged-without-witnesses; Waives-seats: k1 k2 ...; Cutoff: <UTC date>` used as both decision and `--waive-seats` (`waiver_only`) closes without a GO row and without seats, records the waiver per item, and checks every Plan-Item commit merged before the row's cutoff. [src/plangraph/keeper/store_waiver.py:312-336,519-554; src/plangraph/keeper/store_runs.py:368-428]
- The gap of the 11-27 list: `_merged_work_commits` refuses `merged_on_default_missing` for an item without a `Plan-Item` trailer commit (10 of 28), and the incident key RT-P1-EXEC-OWNERSHIP was refused `operator_seat_waiver_scope_mismatch` because D-6142 does not list it (needs a new row, not code). [src/plangraph/keeper/store_waiver.py:257-282]
- `context_sources.named_merge(git, project, sha, head=...)` already verifies a named commit on the default ref and returns `{head, pr, landed}`; the GO path uses it through a `Merge-Repo:` note line. [src/plangraph/keeper/context_sources.py:563-619; src/plangraph/keeper/store_waiver.py:162-173]
- Per-project default branch already exists: keeper config `repos.<project>.ref` (`GitAdapter.repo_ref`, default `main`). Server #28 merged into main at 409a5f49, so the item's part (3) needs no code. [src/plangraph/keeper/evidence_git.py:237-244]
- Write verbs that check the role themselves go in `ROLE_CHECKED_VERBS`; `api.py` (795 lines) is not touched. [src/plangraph/keeper/api_routes.py:31-46,340-350,501-512]

## Decisions

- D1. New store verb `release_review(principal, key, epoch, decision_ref, *, dry_run=True)` in `store_verbs.py`, served as `POST /v1/release_review` through `ROLE_CHECKED_VERBS` and added to `WRITE_VERBS`. Callers: router, or an `l1` that `_may_manage` the item's project. Every other role: `forbidden`.
- D2. Preconditions, checked in this order, before and again under the write lock: `decision_ref` matches `D-[0-9]+` and `_verify_evidence(item, "decision", {"id": decision_ref}, now)` is ok (else `validation` / `evidence_unverified`); state is `in_review` (else `bad_state`); `_epoch_ok(item, epoch)`; `lease_until` is null or in the past (else `conflict: live_lease`); the newest event of the key is at least `STALE_REVIEW_HOURS = 48` hours older than `now` (else `bad_state: recent_activity <iso>`); the item has no `Plan-Item` trailer commit on its default branch, read with `_work_indexes({project}, fresh=True)` outside the lock and `_indexed_trailer_work(..., trailers_only=True)` (else `bad_state: merged_on_default; use close-merged`).
- D3. A preview (`dry_run=True`, the default) returns `{"key", "state": "in_review", "next_state": "open", "would_release": true, "idle_since": <iso>}` and writes nothing but the audit row. Apply calls `_release_claim` with payload `{"decision_ref", "from_state": "in_review", "idle_since"}`; the reply is `{"key", "state": "open", "released": true}`.
- D4. `close_merged` gains `merge_sha` (40 hex) and `merge_pr` (int >= 1). Both are accepted only on the waiver-only path (`waive_seats == decision_id`); otherwise `validation: merge_sha and merge_pr need the operator waiver row as the decision`. `merge_pr` without `merge_sha` is `validation`. The GO path is unchanged.
- D5. In `_merged_waiver_only`, a `merge_sha` is verified with `context_sources.named_merge(git, project, merge_sha, head=item.get("_close_head"))`; `None` refuses `evidence_unverified: merge_sha_not_on_default <sha12>`. A named PR that differs from `merge_pr` refuses `evidence_unverified: merge_pr_mismatch`. Its proof is `{"pr": named["pr"] or merge_pr, "head": named["head"], "decision_id": waiver_id}` with the commit date (`%cI`) of `named["landed"]`, held to the same cutoff test as trailer commits. Trailer commits still count: `_merged_work_commits` runs first and only its `merged_on_default_missing` refusal is turned into an empty list when `merge_sha` is given (an open-lane refusal still refuses). The evidence ref adds `named_merge: {"sha": merge_sha, "pr": <recorded pr>}`.
- D6. With `merge_sha` and a deploy token, `_close_merged` adds `self._merged_go_deploy_changes(item, named head)` to `changes`, so a named commit that ships a unit stays `deploy_pending` as today.
- D7. CLI (`src/plangraph/keeper/cli.py`, commands in `cli_output_verbs.py`): `release-review [KEY] --decision-ref D-N [--epoch E] [--file PATH] [--apply]` and `close-merged [KEY] D-NNN ... [--sha SHA] [--pr N] [--file PATH]`. Exactly one of `KEY` / `--file`. `--file` is JSONL; each non-empty line is an object with `key` and `epoch` (the 11-27 lists have both); `release-review` with `KEY` needs `--epoch`. `--sha`/`--pr` are refused with `--file`. Output is TSV `key\tclass\treason`, classes `WOULD-RELEASE`, `RELEASED`, `WOULD-CLOSE`, `CLOSED`, `REFUSED`; one line per input item, in file order; exit 0 when no line is `REFUSED`, else 2.

## Entry points

- E1. `Store.release_review` via `POST /v1/release_review`.
- E2. `Store.close_merged` with `merge_sha` / `merge_pr` via `POST /v1/close_merged`.
- E3. `pgk release-review` and `pgk close-merged --file|--sha|--pr`.

## Acceptance

| Row | Entry | Observable result | Evidence |
| --- | --- | --- | --- |
| B1 | E1 | A router preview of an in_review item idle 49 h changes nothing; apply moves it to `open` with a `released` event carrying `decision_ref`; `verify_integrity()` is empty. | X1 |
| B2 | E1 | Refusals: live lease (`conflict`), idle 47 h (`bad_state`), state `claimed` (`bad_state`), wrong epoch, unreadable decision, a Plan-Item commit on the default branch (`bad_state: merged_on_default`), worker or l1 of another project (`forbidden`). The l1 of the item's project is admitted. | X2 |
| B3 | E2 | With an OPERATOR waiver row listing the key and no trailer commit, `close_merged(..., waive_seats=D, merge_sha=<landed sha>, merge_pr=N)` previews `would_close`, applies to `done`, and the evidence ref holds the waiver and `named_merge`. | X3 |
| B4 | E2 | Refusals: sha not on the default ref, PR mismatch, `merge_sha` on a GO decision, merge at or after the cutoff, an open lane. | X4 |
| B5 | E3 | `release-review --file` preview prints exactly one line per file item in order and no write; `close-merged --file` prints one line per item; any refusal exits 2. | X5 |

## Verification

Write X1-X5 first; on the base they fail with `unknown verb` / unexpected keyword / missing CLI command.

- X1. `tests/test_keeper_release_review.py::test_preview_then_release_a_stale_in_review_claim` (World, `to_in_review`, `w.clock.advance(49 * 3600)`).
- X2. `tests/test_keeper_release_review.py::test_release_review_refusals` (parametrized; set `lease_until` by SQL on the fixture row; the merged case uses the `env` fixture of `test_keeper_merge_close_auto` with `env.repo.land(...)`), and `::test_only_router_or_the_projects_l1_may_release`.
- X3. `tests/test_keeper_close_merged_named_sha.py::test_operator_waiver_closes_on_a_named_commit_without_a_trailer` (reuse `waiver_world`, `WAIVER`, `HEADER`, `real_git_world` from `test_keeper_close_merged_scan`, with a merge commit whose message has no `Plan-Item` line).
- X4. `tests/test_keeper_close_merged_named_sha.py::test_named_commit_refusals` (parametrized over the five B4 cases).
- X5. `tests/test_keeper_release_review.py::test_cli_file_runs_list_one_line_per_item` and `tests/test_keeper_close_merged_named_sha.py::test_cli_close_merged_file_lists_each_key`, with a stub session as in `test_keeper_close_merged_scan.py::close_args`.
- X6. Held out: the card's `heldout_check` (close-merged, API verb list, MCP routes) stays green.

## Scope and reuse

- R1. Reuse `_release_claim`, `_may_manage`, `_epoch_ok`, `_verify_evidence`, `_indexed_trailer_work`, `named_merge`, the existing operator waiver row parser and cutoff check, and the existing deploy gate. No new waiver row format.
- N1. No change to `release`, `reap`, the automatic merge close, the GO path, `api.py` or keeper config. Per-project default branch is config (`repos.<project>.ref`). Writing the operator waiver row and running the 11-27 lists are live steps after deploy.

### File budgets

Per-file caps are in the card `line_caps` and the TSV. `store_waiver.py` must end at or below 797 lines: put the named-commit proof in one helper `_named_commit_proof(item, merge_sha, merge_pr, waiver_id)` and keep `_close_merged` changes to the signature, the D4 check, the D6 line and the two `_merged_waiver_only` calls.

### Implementation

- store_verbs.py. `STALE_REVIEW_HOURS = 48`; `release_review` wraps `_release_review` in `self._run(principal, "release_review", key, ...)`; the newest event time comes from `SELECT MAX(ts) FROM events WHERE key=?`.
- api_routes.py. Add `release_review` to `WRITE_VERBS` and `ROLE_CHECKED_VERBS` (`need(body, "key")`, `need(body, "epoch")`, `need(body, "decision_ref")`, `dry_run=body.get("dry_run", True)`); pass `merge_sha=body.get("merge_sha")`, `merge_pr=body.get("merge_pr")` in the `close_merged` router entry.
- store_waiver.py. Signature changes per D4; `_merged_waiver_only(..., merge_sha=None, merge_pr=None)` per D5; D6 line.
- cli.py / cli_output_verbs.py. Parsers and commands per D7; reuse `_close_merged_with_read_back` for a single key and the from-open TSV printer for `--file`.
- docs/KEEPER.md. Verb table row `release_review(p, key, epoch, decision_ref)` (router, project l1; `in_review`->`open`), the D2 preconditions, `--sha`/`--pr`/`--file` on close-merged, and that a project's default branch is `repos.<project>.ref`.
- changelog.d/LE-NO-KEEPER-VERB-FOR-AN-OPERATOR-ORDERED-STALE-CLAIM-AND-MERGED-CLEANUP.md. One line.

## Open points for design approval

None.
