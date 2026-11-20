# Checkpoint size and review convergence

Interim PHASE-BATCH-SIZE-EXPERIMENT budget: **under 100,000 bytes AND at most
25 files** per committed PR diff. Either breach refuses packaging. This is a
conservative operational bound, not an experimentally established optimum.
The Rust `checkpoint-size BASE HEAD` command in `pg-hook` enforces it. The
[phase-batch skill](../../skills/phase-batch-orchestration/SKILL.md) requires it
before pushes and seats, and describes stacked checkpoints. Existing hook
installation and non-checkpoint pushes are unchanged.

## Batch size is computed

`plangraph batch-size INPUT.json` (`rust/plangraph-cli/src/batch_size.rs`) prints how many
items one batch brief carries and why; the brief names both. The count is the smallest of:
the model cap (10 for a strong writer, catalog intelligence 50 or more, with a window of
400k tokens or more; 5 for a strong writer below that window; 3 otherwise), the fit (the
items' expected diff bytes, in order, kept under 60% of the limit so the fix rounds still
fit; the estimate of an item is its own, else the median of merged items of its kind) and
the feedback of the last batch (one fewer after skipped items or a HIGH that survived two
rounds, one more after a batch accepted in round 1). An item expected above half the limit
goes alone. The limit is an input (default the interim 100,000 bytes above); the ledger
(LEDGER-AUTO-FILL, MERGE-TELEMETRY-LEDGER-D6311) gives rounds to ACCEPT by diff size, and
the experiment item sets it from that measure.

## Evidence slice: 2026-11-13 and 2026-11-14 UTC

Snapshot collected 2026-11-15 from GitHub for the eight PRs named in the item.
Dates are PR creation dates. Bytes below are GitHub's returned diff bytes at
collection time, not reconstructed historical review-head sizes. The command
uses a conservative full-index binary diff, so its byte totals can differ.
Review outcomes are from the supplied plan-order-0509 account (2026-11-14
05:5xZ); GitHub returned **zero submitted reviews** for every sampled PR.
Native seat verdicts are not GitHub review submissions. Missing ACCEPT and
round data are censored observations, never zero-round successes.

| PR | Created UTC | Files | Diff bytes | Rounds to ACCEPT / observed outcome |
| --- | --- | ---: | ---: | --- |
| #861 | Nov 13 | 10 | 43,642 | 1–2, merged (item reports range, not exact round) |
| #862 | Nov 13 | 22 | 70,051 | No ACCEPT reported; HIGH from both seats after 6 writer rounds |
| #864 | Nov 13 | 11 | 51,636 | No ACCEPT reported; past cap, exact rounds unavailable |
| #865 | Nov 13 | 189 | 499,363 | No ACCEPT reported; all 4 seats requested changes on 98ab0dcd |
| #866 | Nov 13 | 41 | 135,658 | No ACCEPT reported; past cap, exact rounds unavailable |
| #868 | Nov 13 | 90 | 244,944 | No ACCEPT reported; past cap, exact rounds unavailable |
| #870 | Nov 14 | 14 | 28,121 | 1–2, merged (item reports range, not exact round) |
| #879 | Nov 14 | 6 | 19,560 | 1–2, merged (item reports range, not exact round) |

The item described an earlier #862 snapshot as 19 files / +839, and #865 as
501 kB; the API snapshot above reflects subsequent PR state. Do not correlate
those later sizes with exact historical rounds as though measured together.
The smaller #862 and #864 also failed to converge: the budget reduces exposure,
but does not guarantee acceptance or justify increasing the round cap.

Reproduce the API snapshot per PR (`N` is a PR number):

```console
gh api repos/{owner}/{repo}/pulls/N
gh api repos/{owner}/{repo}/pulls/N/reviews
gh api -H 'Accept: application/vnd.github.diff' repos/{owner}/{repo}/pulls/N
```

For PHASE-BATCH-SIZE-EXPERIMENT, still open: census every PR created on both
dates; retrieve native seat/run artifacts and historical heads from
the separate ledger repository (absent here); count one review round per distinct reviewed head,
not per seat, and report the first all-required-seats ACCEPT round or censoring
at closure/cap. Store head, merge-base, bytes, files, round, verdict, run IDs,
tokens and elapsed minutes together. This initial sample supports the interim
guard but cannot establish a causal size/convergence curve or exact rounds for
every PR in the two-day population.
