### Origin-main `decision_ref` close (KEEPER-DECISION-CLOSE)

The alternative `close_on_decision(p, key, decision_ref="D-5024", epoch=E)`
needs no batch file. The router or the **current claimant** may call it; a
non-router must supply the current claim epoch (a delegate alone is not enough).
The item must be `claimed` or `in_review`, and either have tier `none` or kind
`decision` (D-5053); claimants never close `operator_task` (router or operator answer only). This does not bypass review for ordinary
`standard`/`high` work. The `batch_ref` form is a router-only fallback until LIVE-PROBE shows auto-close live (D-5053).

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
