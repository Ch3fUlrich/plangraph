### Origin-main `decision_ref` close (KEEPER-DECISION-CLOSE)

The alternative `close_on_decision(p, key, decision_ref="D-5024", epoch=E)`
needs no batch file. The router or the **current claimant** may call it; a
non-router must supply the current claim epoch (a delegate alone is not enough).
The item must be `claimed` or `in_review`, have tier `none`, and declare exactly
`evidence_kinds: [decision]`, regardless of item kind. A stored `standard`/`high`
decision-only item closes only through review; claimants still cannot close
`operator_task` (router or operator answer only). New decision-only declarations
require the router and tier `none`. Stored reviewed definitions remain readable
and accept edits that do not explicitly set tier/evidence, including automatic
standard-to-high rename escalation; none-tier renames retain tier `none`.
Mixed `[decision, commit]` evidence does not permit a decision-row close.
The `batch_ref` form remains a router-only fallback.

Proof rules (D-5053): fetch and immutable proof outside the write transaction; only keeper item/epoch rechecked inside:

* `decision_ref` is a bare `D-` followed by at least four digits, not a local
  path or branch-qualified reference; do not also supply `batch_ref`.
* Matching `decision` evidence (`ref.id` equals the requested D-id) must be
  attached to the item's current epoch. The authoritative row is checked
  directly, rather than trusting the evidence's cached verification flag.
* The verifier resolves the configured `decisions` repository, fetches
  `origin main`, resolves its commit SHA, and reads `SHA:router/docs/decisions-log.md`.
  Exactly one line must begin `| D-5024 |`, and that row must contain the exact,
  case-sensitive whole token `CLOSE project:ID` for this item. For example,
  `CLOSE demo:AB` cannot close `demo:A`. Local edits and other branches do not count;
  the legacy `Closes:` syntax is not this form's proof.
* The row's SHA-256 (UTF-8 bytes, without its line ending) binds to that fetched commit (D-5053), even if origin/main later moves; no in-lock Git re-read. Authorization, epoch, unlocked state and eligible evidence/tier
  are rechecked under the lock. Mixed evidence is refused even when its commit proof passes.

Success sets `state=done`, records the caller as `reviewer`, and appends the
locking `closed_on_decision` event with `decision_ref`, `decision_ids` and
`row_sha256` and `decision_commit_sha`; those proof fields are also returned to the caller.

CLI usage (after attaching the matching decision evidence):

```sh
pg close demo:A --decision-ref D-5024 --epoch 3
```

API usage: authenticated `POST /v1/close_on_decision` with JSON
`{"key":"demo:A","decision_ref":"D-5024","epoch":3}`. A router may omit
`epoch`; a claimant may not. The Store equivalent is
`store.close_on_decision(principal, "demo:A", decision_ref="D-5024", epoch=3)`.

Refusals: `validation` for a malformed D-id or both reference forms;
`forbidden` for a non-router who is not the claimant; `epoch_stale` for a
missing/stale claimant epoch; `evidence_missing` for no matching current-epoch
decision evidence; `evidence_unverified` for an unconfigured repository, failed
fetch, unreadable log, absent/duplicate row, missing exact `CLOSE` token, or
failed commit proof. Locked items refuse `locked`, inactive or ineligible items
refuse `bad_state`, and an epoch or keeper item-row change during close refuses
`conflict`. Refused closes do not mark the item done.

### Router GO close for merged reviews (D-5504/D-5506)

`close_merged(p, key, decision_id, families)` closes ONE merged `in_review`
item (`standard`/`high`) through `review_passed`, preserving the SQL locks,
token revocation and fold of a passed review. Only the router may call it:

```sh
pg close-merged demo:A D-5504 --family google
pg close-merged demo:H D-5504 --family google --family deepseek
pg close-merged demo:H D-5504  # recover seats from cited mirrored review runs
```

HTTP `POST /v1/close_merged` and MCP `close_merged` take
`{key, decision_id, families}`. The keeper resolves `merged_on_default`
Plan-Item commits to their introducing default-ref merge's second parent
(the PR head), never a later direct follow-up or caller-supplied head.
Missing, unknown or failing Git proof refuses `evidence_unverified: merged_on_default_missing`.
The existing decisions index must verify the D-id; only its DECISION cell
authorizes, beginning with `GO <head>` (8+ hex prefix, optional backticks) or
`GO #PR ... GO-HEAD <sha40>`, optionally prefixed by `(HH:MMZ, date -u)`.
The PR number may be backticked or followed by punctuation (for example,
`GO #786, ...` or ``GO `#786` ...``).
The latter requires exactly one full-length GO-HEAD binding; later review
narrative is not a verdict. A negated or quoted leading GO never authorizes;
the head must uniquely match an actual merged PR head.

Standard needs at least one known cross-writer-family seat; high needs at
least two distinct families. Every supplied family, for either tier, must be
backed by a `pass` seat in a finished `record_run` with `rc=0`; a started,
failed or missing run cannot back a family. Model aliases normalize.
Duplicates and any recorded writer family
(commit writers, writer model, owner/claimant/delegate and run writers) refuse.
An Anthropic-family seat counts when no Claude family wrote (D-5529).
Cards tagged `auth` or `secrets` require two non-Anthropic seat families,
even if their item has standard tier; an Anthropic seat cannot replace either.
Stored card tags must be a JSON list of strings from the card schema's exact
lowercase vocabulary (`auth`, `secrets`, `money`, `migration`, `public-contract`,
`live-step`). Family validation and the ordinary tier minimum run first:
too few families refuse `coverage: insufficient_seat_families` before tags
are read. Once that minimum passes, NULL, malformed JSON, non-lists and
invalid entries (including case/whitespace variants) refuse
`validation: card_risk_tags_invalid` before the auth/secrets seat minimum,
writer exclusion and recorded-run backing checks. Invalid tags never skip
the seat gate. A missing card retains the ordinary tier-based gate.
Checks repeat inside the write lock; changed proof-bearing item/head refuses
`conflict`, while lease touches do not.

Success writes exactly one verified `decision` evidence record
`{id, families, merged_head, caller}` and one audit line. Every refusal is
audited without evidence or state changes. All other states, including a
second call on DONE, refuse `bad_state`; all other roles refuse `forbidden`.
This is not a batch op and changes no `verdict`, `claim`, `evidence`, or `close`
rule. The Python MCP schema table lives in `mcp_tools.py` so the stdio
transport stays below its 400-line limit without unrelated formatting churn.

### Stored decision-only definitions and import compatibility

Interactive creates and edits that set `review_tier` or `evidence_kinds` refuse
`[decision]` evidence with a `standard` or `high` review tier. Router imports
preserve these existing definitions and return a warning counting the affected
items; export and replay retain their tier, evidence and state. Unrelated edits
may preserve the stored combination. Setting either field validates the resulting
pair, including a same-value declaration, and checks again under the write lock.

The repository producers were audited as follows:

- `examples/plans/orchard.yaml` (O-1, O-2, O-4, GOAL) and
  `examples/plans/lighthouse.yaml` (L-1, L-2) are historical import examples.
  `tools/split/build_examples.py` imports them to produce `examples/snapshot.json`
  and `examples/rendered/demo.html`. Preserve those inputs and generated contents.
- `tests/fixtures/import_mini/atlas.yaml` (R1, R2, IR1) and `borealis.yaml`
  (A4, A5) deliberately exercise legacy import and round trip; retain them.
  `importer.load_dir` supplies default tiers when absent and `export_yaml` writes
  stored definitions; neither should silently change evidence or tier.
- `tests/fixtures/mini_snapshot.json` (atlas:GOAL), `page_snapshot.json`
  (atlas:IMP4/GOAL, borealis:WP4/GOAL), and
  `tests/fixtures/ui/make_synthetic.py` / `synthetic_snapshot.json` are stored
  display fixtures, including historical reviewed decision-only definitions;
  keep their data rather than make rendering fixtures obey new-create policy.
- `tests/keeper_util.py:defn` preserves caller-supplied evidence and default tiers.
  `tests/test_ready_owner.py:srv` uses it to create a *new* decision-only question:
  its missing `review_tier: none` was a fixture mistake and is now explicit.
  Deliberate invalid declarations in keeper tier/decision tests remain refusals.
- `examples/batch/demo-batch.yaml` creates O-5 interactively: its missing tier was
  a new-create example mistake; it now declares `review_tier: none`.
  `build_examples.py` also emits this batch without changing its declarations.
- `tools/decisions_import.py` and `tools/md_sync_plan.py` already declare tier
  `none` with decision-only evidence; `tools/md_sync_dclose.py` repairs the pair
  together. They need no compatibility exception or data change.

Omit `families` (CLI: omit `--family`) to recover review seats from the
keeper-owned `spawner_records_dir` mirror. Run ids come from the item's `run_ids`
or `run-...` citations in the GO cell. Every cited record must exist uniquely
and bind to this item. Review records must witness a served model, an unpinned,
single-attempt completed review with integer exit zero, the exact merged head,
recorded stop/end_turn, `TEST_GAMING: no` and a final passing verdict.
Implementation records supply no seats. Missing, ambiguous, invalid or
unwitnessed records refuse `NO-WITNESS <key>` without changing state or events.
The close rechecks witnesses under its transaction and records the recovered
run bundle and each family verdict before passing the item atomically.

`pg close-merged KEY D-NNNN --from-open [--family FAMILY]` checks an open or
claimed item without changing it and emits `key`, `class`, `reason` TSV.
Use `--apply` to close after the same checks. This router-only path scans the
complete default-ref Plan-Item history after refreshing the configured remote ref
(`merged_default_ref_refresh_failed` on failure), requires every introducing PR to have
a head-bound GO (additional GO rows must name the same project and item), and refuses unmerged lanes or unavailable lane information.
It uses the same seat rules (including cited-run recovery when families are
omitted), records all PR numbers, heads and GO decisions in evidence, and
atomically submits then passes the item. It does not require a new claim.
HTTP `/v1/close_merged` accepts boolean `from_open` and `apply` for this path.

Historical missing-witness closes remain disabled unless explicitly requested:
`pg close-merged KEY GO-D-ID --waive-seats OPERATOR-D-ID`. The authority must
be a unique indexed decision row with named `CLASS` and `DATE` table columns,
class exactly `OPERATOR`, a DECISION cell beginning
`Waiver: merged-without-witnesses; Waives-seats: KEY KEY ...`.
The space-separated list binds whole tokens only; keys mentioned outside that
list, prefixes, suffixes and comma-separated keys do not grant authority.
Router-class rows, missing ids, duplicate rows and other waiver names refuse.
Every merged PR still needs its head-bound GO; both its merge commit timestamp
and the GO row's `DATE` must precede the cutoff at midnight UTC. The decisions
adapter reads the sole ISO date in table row `| 3.37 | ... |` from the configured
decisions document (the origin version in production); only an absent rule uses
2026-11-13. Duplicate rules, missing/ambiguous dates and invalid dates refuse.
Missing merge dates refuse `operator_seat_waiver_merge_date_missing`; merges on
or after the cutoff refuse `operator_seat_waiver_merge_at_or_after_cutoff`.
An ineligible GO date refuses `operator_seat_waiver_go_date_ineligible`.
A key outside the list refuses `operator_seat_waiver_scope_mismatch`; a GO for
another merge head refuses `go_row_head_mismatch`.
Authority, scope, dates and PR proofs are checked again under the close lock.
Rule 3.37 / D-5865 overrides the two-family rule, including the auth/secrets
requirement for two non-Anthropic families, for the listed keys only.
The item evidence records the waiver class, operator decision id, PR proofs,
`review_tier`, `risk_tags` and `seat_rule_waived_by` (that operator decision id);
no family verdict is invented. HTTP uses optional `waive_seats: "D-NNNN"`.
Supplying `families` together with `waive_seats` refuses `families_with_waive_seats`.
The shared `operator_seat_waiver_invalid_or_ineligible` refusal detail names the
failed decision-id, unique-row/columns, operator-authority, waiver-class/verification
or historical-cutoff check. Omitted HTTP families request recovery; explicit null
is a validation refusal. Synthetic `merged-D-NNNN` bundles are not run citations.

GO stamp prefixes accept `(HH:MMZ)` and `(HH:MxZ)` only, with optional `~`
before the hour and optional `, date -u` before `)`. Hours are 00–23; minutes
are 00–59 or a tens digit 0–5 followed by literal `x`. `(18Z)` is refused.
