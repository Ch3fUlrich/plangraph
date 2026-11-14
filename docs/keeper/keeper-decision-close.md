### Origin-main `decision_ref` close (KEEPER-DECISION-CLOSE)

The alternative `close_on_decision(p, key, decision_ref="D-5024", epoch=E)`
needs no batch file. The router or the **current claimant** may call it; a
non-router must supply the current claim epoch (a delegate alone is not enough).
The item must be `claimed` or `in_review`, and either have tier `none` or kind
`decision` (D-5053), or explicitly declare exactly `evidence_kinds: [decision]`.
That declaration permits legacy decision-only lanes of any tier to close;
claimants still cannot close `operator_task` (router or operator answer only).
Create/edit refuses decision-only evidence with a non-`none` review tier: set
`review_tier: none` or add the required implementation evidence kinds. Only the router may declare decision-only evidence, including by editing an existing `none` item. Without
the declaration ordinary `standard`/`high` lanes retain the refusal, which names
the missing declaration and tier fix. Mixed `[decision, commit]` evidence does
not confer this lane exception. The `batch_ref` form remains a router-only fallback.

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
* The row's SHA-256 (UTF-8 bytes, without its line ending) binds to that fetched commit (D-5053), even if origin/main later moves; no in-lock Git re-read. Authorization, epoch, unlocked state and eligible kind/tier
  are rechecked under the lock. If `commit` is a required evidence kind, the
  existing merged-commit evidence check also runs under the lock.

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
