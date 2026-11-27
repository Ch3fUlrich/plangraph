# LE-NO-KEEPER-VERB-FOR-AN-OPERATOR-ORDERED-STALE-CLAIM-AND-MERGED-CLEANUP

Outcome. A release-review verb returns stale in_review claims to open under a decision id after a preview, and close-merged on the operator waiver row accepts a named commit on the default branch (and a PR number) where no Plan-Item trailer commit exists, both over a list file.

- plan_source. Keeper item plangraph:LE-NO-KEEPER-VERB-FOR-AN-OPERATOR-ORDERED-STALE-CLAIM-AND-MERGED-CLEANUP, operator order 2026-11-27 12:01Z, priority 10, D-7046, D-7048, D-7060, D-7079; incidents 14:18Z and 15:15Z.
- base commit. 443bf493af86a2ccd1d2859fb4e1d52eceea522d
- card SHA-256. e09bbd257267a15c1c8726d9a48484a955d18e59faf52ffe464fdd26ccaddb91
- tree SHA-256. 517a58050094e6264d027175ed2d6616ede3c95efc9fd6c7a0b0e9a17d0b1442
- evidence_after_deploy. After the keeper restart: run `release-review --file the plan-roadmap state file of 2026-11-27 12:01Z (stale claims) --decision-ref D-N` (preview) and then with `--apply`; run `close-merged --file <remaining merged list> D-W --waive-seats D-W` (preview, then `--apply`) on a new OPERATOR `Waiver: merged-without-witnesses` row whose `Waives-seats:` lists the remaining keys and whose `Cutoff:` covers their merges. Record both TSV outputs (counts per class) in the item evidence. Writing the waiver row and the `Release: stale-review-claims` row (D2a) listing the stale keys is routing's step with the operator's D-id.

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
- D2. Preconditions, checked in this order, before and again under the write lock: `decision_ref` matches `D-[0-9]+` and `_verify_evidence(item, "decision", {"id": decision_ref}, now)` is ok (else `validation` / `evidence_unverified`); the row authorizes this release (D2a, else `evidence_unverified: release_not_authorized` or `release_scope_mismatch`); state is `in_review` (else `bad_state`); `_epoch_ok(item, epoch)`; `lease_until` is null or in the past (else `conflict: live_lease`); the newest event of the key is at least `STALE_REVIEW_HOURS = 48` hours older than `now` (else `bad_state: recent_activity <iso>`); the item's repository index is verified: `_work_indexes({project}, fresh=True)` outside the lock returns an index for the project that is neither `None` nor `stale` / `origin_stale`, and its `target` equals the project's default ref re-read with `git.ref_commit(git.repo_path(project), index["ref"])` under the lock (else `evidence_unverified: merge_index_unavailable` / `merge_index_tip_changed`; an unknown index never counts as unmerged); and on that index `_indexed_trailer_work(..., trailers_only=True)["commits"]` is an empty list (a `None` is `merge_index_unavailable`; a hit is `bad_state: merged_on_default; use close-merged`).
- D2a. Release authority. The decision row must carry operator authority (`store_runs._operator_authority(row)` on `decisions.fields(decision_ref)`), its decision cell must start with `Release: stale-review-claims`, and the item key must be one of the space-separated tokens of its `Releases: <key> <key> ...` field (parsed like `Waives-seats:` in `_merged_seat_waiver`: the list ends at the next `Field:` or `;`). A recorded decision of any other class, a non-operator row, or a row that does not list the key refuses. One row may list all keys of a batch; routing writes it with the operator's D-id.
- D3. A preview (`dry_run=True`, the default) returns `{"key", "state": "in_review", "next_state": "open", "would_release": true, "idle_since": <iso>}` and writes nothing but the audit row. Apply calls `_release_claim` with payload `{"decision_ref", "from_state": "in_review", "idle_since"}`; the reply is `{"key", "state": "open", "released": true}`.
- D4. `close_merged` gains `merge_sha` (40 hex) and `merge_pr` (int >= 1). Both are accepted only on the waiver-only path (`waive_seats == decision_id`); otherwise `validation: merge_sha and merge_pr need the operator waiver row as the decision`. `merge_pr` without `merge_sha` is `validation`. The GO path is unchanged.
- D5. In `_merged_waiver_only`, a `merge_sha` is verified with `context_sources.named_merge(git, project, merge_sha, head=item.get("_close_head"))`; `None` refuses `evidence_unverified: merge_sha_not_on_default <sha12>`. A named PR that differs from `merge_pr` refuses `evidence_unverified: merge_pr_mismatch`. Its proof is `{"pr": named["pr"] or merge_pr, "head": named["head"], "decision_id": waiver_id}` with the commit date (`%cI`) of `named["landed"]`, held to the same cutoff test as trailer commits. The verified `named["landed"]` sha also enters the live-evidence floor: `_landing_floor(item, shas=[named["landed"]])` replaces `shas=[]` on the waiver path when `merge_sha` is given, so deploy or live evidence recorded before that landing does not count. Trailer commits still count: `_merged_work_commits` runs first and only its `merged_on_default_missing` refusal is turned into an empty list when `merge_sha` is given (an open-lane refusal still refuses). The evidence ref adds `named_merge: {"sha": merge_sha, "pr": <recorded pr>}`.
- D6. With `merge_sha` and a deploy token, `_close_merged` adds `self._merged_go_deploy_changes(item, named["landed"])` to `changes`, so a named commit that ships a unit stays `deploy_pending` as today, and the floor of D5 decides which deploy rows count.
- D6a. `close_merged` gains an optional `epoch` (int). When given it is checked with `_epoch_ok(item, epoch)` before the scan and again on `fresh` under the write lock, so a list row whose item was reclaimed since the list was written refuses `conflict` instead of closing.
- D7. CLI (`src/plangraph/keeper/cli.py`, commands in `cli_output_verbs.py`): `release-review [KEY] --decision-ref D-N [--epoch E] [--file PATH] [--apply]` and `close-merged [KEY] D-NNN ... [--sha SHA] [--pr N] [--epoch E] [--file PATH]`. Exactly one of `KEY` / `--file`. `--file` is JSONL; each non-empty line is an object with `key` and `epoch` (the 11-27 lists have both) and, for close-merged, optional per-row `merge_sha` and `merge_pr`, which the CLI sends with the row's `epoch` as the request body fields of D4/D6a; `release-review` with `KEY` needs `--epoch`. `--sha`/`--pr` are refused with `--file` (the rows carry them). Output is TSV `key\tclass\treason`, classes `WOULD-RELEASE`, `RELEASED`, `WOULD-CLOSE`, `CLOSED`, `REFUSED`; one line per input item, in file order; exit 0 when no line is `REFUSED`, else 2.

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
| B4 | E2 | Refusals: sha not on the default ref, PR mismatch, `merge_sha` on a GO decision, merge at or after the cutoff, an open lane, a stale `epoch`; and a deploy-shipping named commit whose only deploy row predates `named["landed"]` stays `deploy_pending`. | X4 |
| B4a | E1 | Refusals: an unavailable or stale repository index (`merge_index_unavailable`, never a release), a recorded decision that is not a `Release: stale-review-claims` operator row, and an operator row that does not list the key. | X2 |
| B6 | E1 E2 | Over HTTP (`door_util.Doors`): `POST /v1/release_review` preview then apply as router, `forbidden` for a worker; `POST /v1/close_merged` with `merge_sha`, `merge_pr`, `epoch`, preview then apply. | X7 |
| B5 | E3 | `release-review --file` preview prints exactly one line per file item in order and no write; `close-merged --file` with a mixed list (a trailerless row with `merge_sha`, a row with a stale `epoch`) prints one line per item, closes the first, refuses the second, exits 2. | X5 |

## Verification

Write X1-X5 first; on the base they fail with `unknown verb` / unexpected keyword / missing CLI command.

- X1. `tests/test_keeper_release_review.py::test_preview_then_release_a_stale_in_review_claim` (World, `to_in_review`, `w.clock.advance(49 * 3600)`).
- X2. `tests/test_keeper_release_review.py::test_release_review_refusals` (parametrized; set `lease_until` by SQL on the fixture row; the merged case uses the `env` fixture of `test_keeper_merge_close_auto` with `env.repo.land(...)`), `::test_only_router_or_the_projects_l1_may_release`, `::test_release_needs_a_listing_operator_row` (an unrelated recorded decision and an operator row without the key both refuse) and `::test_unknown_index_never_releases` (monkeypatch `_work_indexes` to return `None` and a stale index).
- X3. `tests/test_keeper_close_merged_named_sha.py::test_operator_waiver_closes_on_a_named_commit_without_a_trailer` (reuse `waiver_world`, `WAIVER`, `HEADER`, `real_git_world` from `test_keeper_close_merged_scan`, with a merge commit whose message has no `Plan-Item` line).
- X4. `tests/test_keeper_close_merged_named_sha.py::test_named_commit_refusals` (parametrized over the B4 refusal cases) and `::test_deploy_row_before_the_named_landing_does_not_count`.
- X5. `tests/test_keeper_release_review.py::test_cli_file_runs_list_one_line_per_item` and `tests/test_keeper_close_merged_named_sha.py::test_cli_close_merged_file_lists_each_key`, with a stub session as in `test_keeper_close_merged_scan.py::close_args`.
- X7. `tests/test_keeper_release_review.py::test_release_review_over_http` and `tests/test_keeper_close_merged_named_sha.py::test_named_close_over_http` (pattern of `tests/test_keeper_merge_close_auto.py::test_a_failed_scan_reports_its_reason_over_http`).
- X6. Held out: the card's `heldout_check` (close-merged, API verb list, MCP routes) stays green.

## Scope and reuse

- R1. Reuse `_release_claim`, `_may_manage`, `_epoch_ok`, `_verify_evidence`, `_indexed_trailer_work`, `named_merge`, the existing operator waiver row parser and cutoff check, and the existing deploy gate. No new waiver row format; the `Release: stale-review-claims` row (D2a) is the one new decision-row class.
- N1. No change to `release`, `reap`, the automatic merge close, the GO path, `api.py` or keeper config. Per-project default branch is config (`repos.<project>.ref`). Writing the operator waiver row and running the 11-27 lists are live steps after deploy.

### File budgets

Per-file caps are in the card `line_caps` and the TSV. plangraph:LE-KEEPER-REDACTED-ITEM-KEY-BREAKS-MERGE-SCAN also writes `store_waiver.py`, `api_routes.py` and `docs/KEEPER.md`, and plangraph:LE-NO-PROGRESS-ALARM-FOR-AN-IDLE-FLEET writes `docs/KEEPER.md`; the keeper's `write_overlap` gate runs them one after another and the final caps hold in either order. `store_waiver.py` must end at or below 799 lines (771 + 24 here + 4 there): put the named-commit proof in one helper `_named_commit_proof(item, merge_sha, merge_pr, waiver_id)` and keep `_close_merged` changes to the signature, the D4 check, the D6/D6a lines, the floor argument and the two `_merged_waiver_only` calls. Put the D2a row parser in `store_verbs.py` beside `release_review`.

### Implementation

- store_verbs.py. `STALE_REVIEW_HOURS = 48`; `release_review` wraps `_release_review` in `self._run(principal, "release_review", key, ...)`; the newest event time comes from `SELECT MAX(ts) FROM events WHERE key=?`.
- api_routes.py. Add `release_review` to `WRITE_VERBS` and `ROLE_CHECKED_VERBS` (`need(body, "key")`, `need(body, "epoch")`, `need(body, "decision_ref")`, `dry_run=body.get("dry_run", True)`); pass `merge_sha=body.get("merge_sha")`, `merge_pr=body.get("merge_pr")` in the `close_merged` router entry.
- store_waiver.py. Signature changes per D4; `_merged_waiver_only(..., merge_sha=None, merge_pr=None)` per D5; D6 line.
- cli.py / cli_output_verbs.py. Parsers and commands per D7; reuse `_close_merged_with_read_back` for a single key and the from-open TSV printer for `--file`.
- docs/KEEPER.md. Verb table row `release_review(p, key, epoch, decision_ref)` (router, project l1; `in_review`->`open`), the D2 preconditions, `--sha`/`--pr`/`--file` on close-merged, and that a project's default branch is `repos.<project>.ref`.
- changelog.d/LE-NO-KEEPER-VERB-FOR-AN-OPERATOR-ORDERED-STALE-CLAIM-AND-MERGED-CLEANUP.md. One line.

## Open points for design approval

None.

## Open review points for the implementer (round 2, final under rule 3.119, D-7092)

The spec is final after two design review rounds (gpt-6.2-sol-high). The points below were still open; the implementer resolves them inside the write set or reports why not.

VERDICT: DESIGN-CHANGE
CITATIONS: checked 10, wrong 0.
FIT/TREE: Covers stale-review release and named-commit waiver close in file mode. The operator rows and live cleanup remain rollout steps. Nine write paths and both digests match.
POINTS: R1 D2 index guard still open, :24,56, src/plangraph/keeper/store_context.py:344–368,392–410. Only the owning project is required fresh and pinned, while the search can silently skip an unavailable second repository holding the qualified trailer. Validate every repository that can hold the item’s merge and test a failed secondary index. BLOCKS
POINTS: R1 D2 operator authority fixed at :25,47,56. The decision row must carry operator authority, be a release row and list the item key.
POINTS: R1 D5–D6 still open, :28–29, src/plangraph/keeper/store_waiver.py:541–548 and src/plangraph/keeper/store_close.py:524–534. The named SHA reaches the live floor, but `_deploy_pending` still accepts verified unit evidence without that floor. Apply the floor to the unit-evidence check and make X4 use an otherwise valid pre-landing row. BLOCKS
POINTS: R1 D7 still open, :30–31,75, src/plangraph/keeper/api_routes.py:340–347 and src/plangraph/keeper/store.py:396–399. Per-row evidence is specified, but the route instructions omit `epoch`, and `_epoch_ok` returns `epoch_stale`. Forward `epoch`, align the refusal code, and assert a wrong epoch is refused over HTTP. BLOCKS
POINTS: R1 X1–X5 HTTP coverage fixed at :48,60. Both endpoints have real preview/apply cases.
