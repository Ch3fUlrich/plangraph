# Native writer progress admission (ROUND-CAP-AUTO-REPLAN, D-5964)

`pg-spawnd` tracks identified implement runs in its shared state directory.
The item comes from `card.item_key`, `card.item`, or `PLANGRAPH_SPAWN_ITEM`.
Each real writer record carries `writer_round`, `round_pr`,
`round_verdict_file`, `narrow_fix`, and `previous_confirmed_blockers`.
Review runs, dry runs and refused launches do not consume writer rounds.
Pre-existing records without `writer_round` are not backfilled. Unidentified
runs retain their existing behavior. This gate is native-spawner only; the
Python spawner and keeper review-seat rounds are not changed.

Give each writer card an absolute `round_verdict_file` output path and,
when available, `pr` as a stable PR URL string. After reviewing that writer,
write a JSON verdict there, using the returned writer run ID:

```json
{
  "writer_run_id": "run-0123456789abcdef",
  "item_key": "plangraph:EXAMPLE",
  "pr": "https://forge.example/project/pull/1",
  "verdict": "REQUEST_CHANGES",
  "findings": [
    {"severity": "MED", "confirmed": true, "clear_fix": true,
     "text": "The complete confirmed finding, verbatim."}
  ]
}
```

Use `pr: null` if the card has no PR. Item and PR identities must remain
consistent across the writer history; changing PR does not reset an item.
The orchestrator confirms findings; the spawner counts only `confirmed: true`
HIGH and MED entries. LOW and unconfirmed entries remain in the snapshot.
Each finding needs nonempty text and a boolean `confirmed`; `clear_fix` is
optional and defaults to false. `ACCEPT` requires no confirmed blockers and
prevents further writer admission for that item. Findings are retained verbatim.
Missing, malformed, oversized, or differently bound verdicts refuse admission;
counts supplied directly on the card are never trusted.

Before the next writer starts, admission reads prior verdict files and saves
immutable `runs/<writer-run-id>/round-verdict.json` snapshots. Later edits to
source verdicts cannot rewrite progress. A per-item lock in the state directory covers admission
through run-record creation; overlapping admission refuses `round_admission_busy`,
and an unfinished writer refuses `round_in_flight`. Different items admit independently.
Prompt-only directories left by incomplete launches are skipped; malformed existing
records refuse admission. Runs for an item must use
one shared state directory. Copying state between hosts is not implemented.

A count must be strictly below every earlier count to reset the miss streak.
Thus `3,3,4` refuses writer 4 with `no_progress`; `4,2,1` admits writer 4;
`2,1,2,1` refuses writer 5 with `no_progress`. Otherwise writer 5 refuses with
`round_cap`; writer 6 always refuses with `round_cap`. At a stop only,
`card.narrow_fix: true` permits one additional narrow round if the latest
verdict has exactly 1–2 confirmed MED, every one has `clear_fix: true`, and
there are no confirmed HIGH. No further writer is admitted after that round.
Refusals persist a refused run with the reason and item, without consuming a round.

Automatic replan is deferred to a follow-up: consume a durable stop event,
select the strongest available planning leg, read the PR diff, item and every
open finding, close the PR unmerged, and use keeper define to create slices of
at most 800 changed lines excluding Cargo.lock and generated files. Preserve
findings verbatim and report one routing line after all effects succeed;
retry safely without duplicate jobs, PR closes, slice definitions or reports.
Until then, the orchestrator performs that stop procedure from R-batch-04.

A spawner-owned `round_void: true` excludes failed rounds that never launched
(no PID), or whose bounded Git fingerprint proves HEAD, tracked diff and
untracked contents unchanged from admission. Voided attempts consume neither
rounds nor progress counts; they never introduce a zero baseline. Successful
runs and failures with changed files still need review. Unavailable fingerprints
stay conservative; gitconfig guard failures are never voided. The card cannot
set this marker. Use one writer per checkout; unrelated concurrent edits can
prevent the unchanged proof, so review that diff rather than inventing a count.

