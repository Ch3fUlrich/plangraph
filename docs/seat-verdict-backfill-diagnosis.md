# Seat verdict backfill diagnosis

Historical diagnosis: the observations and implementation gaps below describe the
original saved dry-runs, not the current forward path. Slice A (PR #734), now on
main, supplies the completion-to-verdict bridge and its independent delivery
outbox. Historical backfill uses that same locked outbox to avoid replaying a
forwarded or ambiguous verdict; it does not replace the bridge or its parser.

## CI timeout diagnosis

`tests/test_route_round3.py::test_changelog_fragment_only` requires the shared
`CHANGELOG.md` to match main. The backfill entry changed that large ledger;
pytest's verbose failure rendering then spent minutes in `difflib._fancy_replace`
while the remaining workers reached 99%. This was CPU-bound assertion reporting,
not a keeper call, network request, or sleeping review test. Keep the entry in
`changelog.d/SEAT-VERDICT-TO-KEEPER-r2.md` and leave the shared ledger unchanged.

## Observed counts

The saved initial dry-run inspected 177 in-review items and accepted zero:
131 were rejected for unknown writer families and 46 for missing or ambiguous
evidenced commits. Two subsequent saved dry-runs still accepted zero, with:

| Reason | Items |
| --- | ---: |
| No matching record | 111 |
| Unknown writer | 26 |
| No commit evidence | 31 |
| Ambiguous verified commits in the same epoch | 9 |
| Total | 177 |

The operator's separate measurement reports 147 items with merged evidence.
That is not an eligibility count: merged evidence alone does not establish a
completed, independent review of the evidenced head. These saved runs do not
measure how many seat records lack `item_key`; that frequency remains unverified.

## Cause and current base

Historical seat records commonly lack the item identity needed for a direct
join. An item-key-only join therefore loses reviews that name only a PR in
their prompt. The current base already supports prompt item identity and PR
number fallback in `eligible_records`, with conflicting identities rejected.
PR discovery is bound to the evidenced commit, not arbitrary PR comments.

Remaining gates explain why adding the fallback need not make the result
nonzero: writer attribution, commit selection, PR resolution, record provenance,
completed status, exact head binding, family independence, and parsed verdict
all remain required. A missing record must name the required review, outside
writer families, evidenced head, item, and resolved PR where available.

The spawner annotates spawn records with item identity. Its terminal save path
delivers completion events, but does not invoke the seat-verdict bridge.
Delivery of a completion notification must not be counted as delivery of a
reviewer verdict.

## Forward path and safe backfill contract

1. Preserve validated item identity on every new review spawn and completion.
2. After persisting a successful terminal review record, invoke the existing
   verdict parser and provenance verifier against the item's evidenced head.
3. Require a witnessed served family outside the writer families. Submit only
   through that family's reviewer principal; never reuse a router principal.
4. Persist verdict delivery separately from completion delivery. Retry failures
   without treating a failed submission as delivered; verify replay behavior.
5. Run discovery over a frozen snapshot and record index. Emit one outcome per
   in-review item, including the concrete missing-seat requirements.
6. Resolve historical reviews through prompt identity or a uniquely evidenced
   merged PR, rejecting contradictory keys, PRs, or heads.
7. Spawn missing seats through normal free-leg selection, without model pins.
   Record the returned run identifier beside each unresolved item.
8. Apply only after an authorized operator enables keeper writes. Re-snapshot
   afterward and verify the merged-evidence remainder is at most 20, with every
   remaining item naming its missing seat or spawned run.

No live verdict application, seat spawning, merge, or deployment was performed
for this diagnosis. The forward completion bridge remains implementation work;
the existing discovery fallback is not evidence that it is live.
