# Throughput telemetry

`plangraph throughput` extends the LAND-MODE-MEASURE SQLite database. Set
`PLANGRAPH_LANDING_DB` to the file used by the lander or use its default
`${XDG_DATA_HOME:-$HOME/.local/share}/plangraph/landing-telemetry.sqlite3`.
The collector requires an initialized landing table keyed by repository and
landed SHA. The CLI uses bundled SQLite. It stores UTC hourly and daily rows
in that file without changing the lander's rows.

Run collection from the repository whose main history is being measured. Use
the same forge repository identity as the lander and a keeper snapshot covering
the measurement period. To load the two baseline days through the reported
cutoff, use a main ref observed through that cutoff.

```sh
plangraph throughput collect "$FORGE_REPO" "$KEEPER_PROJECT" refs/remotes/origin/main \
  "$KEEPER_SNAPSHOT" 2026-11-20T00:00:00Z 2026-11-21T17:30:00Z
plangraph throughput report "$FORGE_REPO"
```

Collection counts all commits made reachable by each first-parent update,
including its merge commit. Keeper closures count unique done items from the
selected project by `done_at`, including closures made outside a merge.
The report also counts unique `Plan-Item` trailers newly reachable on main
and attributes each item to its earliest collected landing. The
report includes observed seconds so a partial UTC day is visible. Repeating or
extending a collection replaces buckets by key. An earlier cutoff cannot
replace a later observation of the same bucket. The weekly comparison returns
both seven-day windows with observed seconds and commits per observed day.

Train counts, part counts and elapsed build-to-land time come from the
existing landing table. Set `PLANGRAPH_THROUGHPUT_RUNS` to a path list of runs directories
containing native writer and associated reviewer records. The collector joins
ACCEPT to the exact landed head or the corresponding merge's second parent,
uses the review timestamp or the reviewer end record, and pairs attributable
commits with recorded writer tokens. Writer active share divides time in Codex
turns by measured writer-run time. Older runs without observed turns contribute
running time without contributing to that share. Set
`PLANGRAPH_THROUGHPUT_QUOTA` to a JSON array exported by the quota snapshot
reader to record provider headroom. Unmatched records and unavailable historical
observations remain unknown.

The hourly collector reprocesses the previous two completed UTC hours and
catches up from its last bucket after downtime. The shipped unit starts a
new collection on November 20, 2026. Set `PLANGRAPH_THROUGHPUT_START` to
override that date for a new repository. Each tick advances by at most 31 days.
Before enabling `pg-throughput.timer`, configure the lander's database,
forge repository, keeper project and keeper snapshot in
`~/.config/plangraph/throughput.env`. The service reads these last three
values from `PLANGRAPH_THROUGHPUT_REPO`, `PLANGRAPH_THROUGHPUT_PROJECT` and
`PLANGRAPH_THROUGHPUT_SNAPSHOT`. The main ref and published keeper snapshot
need to be refreshed by their owners.

## Parts, landing rows and quota windows

An arrival joins one acceptance per verified included part: every ACCEPT of a writer's final head that is a commit
the arrival made reachable (a train of six parts gives six rows in `throughput_accept_part`, keyed by landed SHA
and part head). Each row counts the commits only that part brought, oldest part first; the train's own merge
commits belong to no part. The report's `trains` rows give `accepted_parts`, the mean wait, `attempts`
(1 + `ci_reruns`), `drops` (`left_out`), `fix_tasks` and `minutes_per_merged_batch`; `hours` gives commits, closed
items, items landed and batches merged per UTC hour. A landing database without one of those lander columns
reports it null, and `attempts` is null when `ci_reruns` is. Red runs, one row per batch and fix rounds by cause are not in the landing row and stay unread (slice THROUGHPUT-BATCH-ROWS).
Quota rows (`throughput_quota_window`) hold provider, account, window, the percent and the reset the snapshot
names; a missing reset or a stale snapshot is null. Rows an earlier collector wrote to `throughput_quota_hourly`
and `throughput_accept` are carried into the new tables by the next collection (`INSERT OR IGNORE`, once; the old
tables stay), and a report made before that collection reads them from the old tables.

Each landing in a collected window is joined from the landed history, at the landing's own time. Its train is every
commit reachable from its landed SHA and not from its base: in the merging modes (`train-pr-merge`, `single-pr`)
the first parent of the merge commit, in `tested-ff` the latest recorded landing in the landed SHA's ancestry (the
one no other ancestor landing descends from; the landed SHA must lie on the selected main ref; none means no
verified base). The parts it held are the second parents of the merges on the first-parent chain from that base to
its tip (the lander builds a train by merging each part's head, so a head that reached main on its own is no such
parent), and their number must equal the number the lander recorded, else nothing is attributed. Only a part head
with an accepted review gets a row; `parts_complete` is 1 when every part head has one (never with zero reviews).
A report row keeps every verified sample, and `accepted_parts` is unknown only when there is neither a sample nor a
complete join. A tested fast-forward, whose parts' merges reach main under SHAs and commit times of their own, and a
train that began before the window both join all their parts. A part head has one row (`repo`, `head`); a
first-parent arrival never replaces the row of the train that carried it. Rows migrated from the first collector are
marked legacy and dropped once verified part rows exist for their landing, so re-collecting does not count a landing
twice and collecting another window never brings a retired sample back. Hourly counts of items landed and batches
merged stop at the hour's observed end (`through`), so a landing after a partial-hour cutoff is not counted.
