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

For a saved `verdict.json` with `status: retry` and reason `keeper verdict transport not configured`, explicitly re-post with `python3 tools/seat_verdict.py --key K --seat-dir RUN --url URL --token-dir DIR [--head FULLSHA]` after configuring transport. The command uses the record's `given_writer_families` and completion head by default, rechecks the existing identity, head, family and PASS gates, and shares the locked outbox; keeper acknowledgement marks delivery so another invocation cannot replay it. It refuses terminal or ambiguous sends rather than treating them as transport retries. If the recorded writer list is absent, use the normal explicit `--writer-family`/`--head` manual path only after operator reconciliation.

## Round 1 fail-closed backfill corrections

Historical records with `pin`, `pinned`, or `spawn_pin` indicators in the record,
plan, or chosen leg are refused as `pinned seat`. The spawner's actual explicit
selection witnesses (`plan.p_source: override` and `override_reason`) also refuse
the seat. Successful completion and integer-zero exit status remain mandatory;
new regressions cover failed, interrupted, running, and nonzero-exit records.
Discovery derives independence from the evidence reader's witnessed served model,
including the supported request-side free-leg witness, rather than a second raw
model lookup. Missing strict verdicts and writer-family seats remain reasoned skips.

When eligible seats disagree, a negative verdict always wins over an acceptance.
The same negative seat stays selected on replay even when its outbox is terminal,
ambiguous, or busy; acceptance is never a fallback. A corrupt, non-object, unknown,
or invalid-attempts delivery journal is an `ambiguous verdict journal` skip for
that item only. JSON apply rows report `delivery` separately from eligibility and
include the verifier exit code on delivered or failed attempts; TSV reports the
delivery result and skip reason too.

The shared outbox lock uses nonblocking acquisition (`verdict lock busy`), and
local Git and optional network PR subprocesses have 30-second timeouts. Symlinks
within the records root are refused for both review and writer discovery, without
rejecting unrelated symlinked system ancestors. Malformed evidence collections
produce `no commit evidence` rather than aborting the snapshot. The unused
`card_head_matches` helper and its decorative mock were removed: the production
contract is exact completion-head equality, not an ancestry-based relaxation.

## Round 2 attribution and outbox isolation

Only review records positively bound by item key, named prompt item, or evidenced
PR contribute item rejection reasons. Contradictory identities on a bound record
still refuse; unrelated keyed records do not suppress the concrete `no record`
requirements or pollute TSV reason tallies. Writer families are the union of Git
trailers and matching implementation-record witnesses, never whichever source was
found first. Unknown writer witnesses fail closed rather than shrinking coverage.

Record discovery uses `os.walk(followlinks=False)` and prunes symlinked directories.
Each pruned directory has an explicit stderr diagnostic separate from item reasons;
discovery does not read through it to guess an item binding. Unrelated symlinked
system ancestors remain supported. Pin-key presence refuses even null, empty, or
numeric-zero values; only explicit boolean `false` is non-refusing. JSON rows in
both modes always carry `delivery` and `verifier_exit`, initially null. Direct
library calls cannot bypass explicit apply or combine apply with either dry-run.

Each seat's lock creation, locking, journal access, verifier invocation, and outcome
persistence are isolated from later items. Unexpected errors produce a failed row
with a phase and exception type without logging potentially sensitive exception
text. Poster crashes and invalid exit values preserve `sending`; a journal already
persisted as terminal also remains non-replayable if its marker write fails.
Regressions cover JSON and TSV batch continuation, preserved healthy outboxes, and
the forward path's actual `notify.inbox_lock` contending with backfill before a
single successful send. Existing CHANGES-first selection remains unchanged.

## Fail-closed lookup and transport preconditions

An exception, empty result, or missing writer trailer from a configured Git lookup
refuses attribution as `unknown writer`; implementation records cannot replace that
missing Git evidence with a smaller writer set. PR/writer resolution errors produce
a reasoned missing row for that item and do not prevent later items from finishing.

Apply checks for a URL and token directory before inspecting or creating the shared
outbox journal or lock. Missing transport reports `delivery=failed` with a reason
and leaves the forward outbox unchanged. Library discovery uses the same default
records directory as the CLI when `records_dir` is absent or null. Explicit batch
flags reject single-seat identity and writer arguments instead of ignoring them.
An absent library `url` uses `PG_URL`, matching the CLI; a null or empty URL still
refuses delivery, as does an absent/null token directory.

Prompt identity scanning accepts complete project/item tokens, including dotted,
underscored, and lowercase identifiers, only within projects known from the snapshot
or the current item. Tokens such as `model:GPT-4`, `Branch:MAIN`, and `Team:AI` do not
become contradictory item keys merely because their suffix is uppercase; actual
contradictory keys in known projects still refuse the bound record.
