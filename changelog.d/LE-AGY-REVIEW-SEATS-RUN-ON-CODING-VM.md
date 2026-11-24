`plangraph review` places the seats of build-capable legs (agy, claude, codex: any leg that is not
inline-only or opencode) on a worker host: the seat card carries `review_workspace` and
`repository_review_required`, and the spawn call no longer passes `--cwd` (an explicit checkout pinned
the seat, and its cargo and pytest runs, to the host that ran the review). The spawner picks the worker
host by free RAM and disk, cuts the review checkout there and records `placement` with the reason; the
host that runs the review is only the logged last resort. A spawner dry run of a workspace review now
names the worker host in the same `placement` field, so a review dry run shows where the seat would run.
With no registered worker host the spawner refuses a workspace review with `review workspace requires a
registered worker host`. Inline-only legs keep the repository checkout.
The plan-order `bin/review.sh` is not in this repository: it must drop its `--cwd .../checkout` and send
the same card fields.
