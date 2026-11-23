# Batch step contract v1

A batch record is a JSON object with `version: 1`, the repository identity
`repo`, a stable `batch_key`, and an ordered `rows` array. Each row records
one attempt as `{ "step": ..., "stage": ... }`. See the
[wire types](../rust/worker-core/src/batch_steps.rs) and the
[validated claim-to-train-join example](../rust/worker-core/tests/batch_steps.rs).

| Step type | Kind | Inputs | Completed output |
| --- | --- | --- | --- |
| `claim` | Deterministic | `items`, the ordered plan-item keys | `claim`, the batch claim reference |
| `worktree` | Deterministic | `claim`, pinned Git `base` | `worktree`, a handle to the prepared checkout |
| `write` | Agent | `worktree`, `brief` reference | committed `head`, resumable writer `session` |
| `fast_checks` | Deterministic | `head` | `stamp`, the passing checks receipt for that head |
| `review` | Agent | `head`, passing `stamp` | `verdict` (`accept` or `request_changes`), `review` evidence reference |
| `fix` | Agent | reviewed `head`, original writer `session`, `review` with findings | updated committed `head` |
| `push` | Deterministic | accepted `head`, `stamp`, accepting `review` | `pr`, the pull request number |
| `ci_class` | Deterministic | pushed `head`, `pr` | `classification` (`green` or `red`), CI `run` ID and `attempt` number |
| `train_join` | Deterministic | green `head`, `pr`, `run`, `attempt` | `train` identifier and `manifest` reference containing the admitted part |

A `step` object contains its `type`, the named input fields, and a `result`.
A completed result is `{"status":"completed","output":{...}}` with the output
for that type. A failed result is `{"status":"failed","reason":"..."}` and
leaves the next step pending. A negative verdict or red CI is a completed
classification with evidence.

The `stage` object contains `step_index` (zero-based position in the batch),
`attempt` (one-based within that position), `started_at_ms`, and
`ended_at_ms` (Unix milliseconds). The row's identity is
`(repo, batch_key, step_index, attempt)`. The step supplies the row's type,
head where available, outcome, and receipt or reason. An attempt starts at or
after the preceding attempt ends. Preserve each finished row in order. A retry
after failure keeps the step index and increases the attempt; completion
advances the index and begins the next step at attempt 1.

The sequence begins with claim, worktree and write. Fast checks and review
bind to the resulting head. A request for changes sends the findings and that
head back to the original writer session. The updated head goes through fast
checks and review again. Push consumes the stamp and accepting review for
the same head, and train join consumes the green CI run and attempt for the
same pull request and head. Red CI ends this sequence at classification.

References identify evidence supplied by each step. Validation checks the
record shape, sequence and consistency of those references. Consumers verify
the referenced evidence against the authoritative source before acting.
