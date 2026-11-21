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

The stop rule has one owner, `stop_reason` in `rust/pg-spawnd/src/round_progress.rs`; the
Python setting, the batch-writer skill and R-batch-04 quote it. A count must be strictly
below every earlier count to reset the miss streak, and round 1 counts as a miss until a
later round is lower. 3 rounds in a row without a new best refuse the next writer with
`no_progress` (`5,5,5` refuses writer 4; `6,4,3,2` admits writer 5). From round 3 on a
count above half of the round-1 count refuses with `slow_progress` (`8,7,6` refuses
writer 4). Writer 9 always refuses with `round_cap`. At a stop only,
`card.narrow_fix: true` permits one additional narrow round if the latest
verdict has exactly 1–2 confirmed MED, every one has `clear_fix: true`, and
there are no confirmed HIGH. No further writer is admitted after that round.
Refusals persist a refused run with the reason and item, without consuming a round.

A refusal with `no_progress` or `round_cap` that carries `card.pr` queues one durable replan job
(`rust/pg-spawnd/src/round_replan.rs`, one file per item and PR under `replans/` in the spawner
state; a repeated refusal finds the file and queues nothing; the refused run records
`replan: queued`). The job runs four saved steps: the strongest available planning leg of
the route groups the PR's changed files into slices of about 1500 changed lines (a target,
rule 3.35; `Cargo.lock`, other lock files and generated files do not count; a plan that
drops or repeats a file is refused); the slice items `<item>-S<n>` are defined through keeper
define, each with every open finding verbatim; the PR is closed unmerged; one routing line
`REPLAN <item> <reason> | PR <pr> closed unmerged | slices <keys>` is sent. Each step is
saved before the next and skipped when saved, so a retry never repeats a definition, a close
or the report; the report is marked before the send and un-marked only when the send failed,
so a crash between the mark and the delivery sends the line again, under one idempotency key
(32 hex, stable per item and PR; `fleet-msg send --msg-id`) the receiver dedupes, and the job
is `sent` only after the delivery was acknowledged. The engine takes its legs, PR, keeper and
routing through the `Ports` trait; `round_replan_ports.rs` binds them to the spawner route,
`gh pr view|close`, the keeper CLI (`show`, `define`) and the fleet bus client, and
`pg-spawnd serve` runs a pass over the queued jobs every 30 s. The production planner adopts
the deterministic grouping; the strongest planning leg is named in the job file.

A spawner-owned `round_void: true` excludes failed rounds that never launched
(no PID), or whose bounded Git fingerprint proves HEAD, tracked diff and
untracked contents unchanged from admission. Voided attempts consume neither
rounds nor progress counts; they never introduce a zero baseline. Successful
runs and failures with changed files still need review. Unavailable fingerprints
stay conservative; gitconfig guard failures are never voided. The card cannot
set this marker. Use one writer per checkout; unrelated concurrent edits can
prevent the unchanged proof, so review that diff rather than inventing a count.

Progress tests use the shared isolated Rust Git fixture command; inherited hook repository variables cannot redirect fixture commits into the pushing checkout.

The native-codex worker's in-session review loop applies the same stop rule:
`native_writer::round_stop` calls `round_progress::stop_reason` with the HIGH+MED
counts of the REQUEST_CHANGES rounds so far, for the round just finished, and
caps the run at `round_progress::MAX_ROUNDS` review rounds (see `docs/native-codex-writer.md`).
