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

## Running a list

`pg-spawnd step-run RUN_ID --steps FILE --repo OWNER/NAME --batch-key KEY --items A,B --base SHA --brief FILE --train-cmd "ARGV" --ci-workflow NAME...`
runs the list of one batch. `RUN_ID` is the native run whose record holds the worktree and
the writer card. A missing steps file starts the list; an existing one is validated and
resumed after its last recorded row, so a killed run repeats no finished step. The steps
file is rewritten after every attempt. `--train-cmd` and at least one `--ci-workflow` are
required, so a list never runs to the end of CI before failing for want of them. Optional
flags: `--keeper-cmd "ARGV"` (default `pg --json`), `--branch`, `--state`, `--no-journal`.
Exit 0 means the part joined a train, 3 red CI, 1 any other stop.

The agent host is the native writer Host: `worktree` is its checkout (it pins the base the
review reads), `fast_checks` its gates, which write the stamp the review seat reads, and
`push` verifies that same stamp before publishing the head. `write`, `review` and `fix`
are its writer turns and review runs; the review state (previous review, HIGH+MED count of
each round) lives beside the steps file, so the delta review and the round stop rule
(`round_progress`: no progress, slow progress, cap of 8 reviews) survive a restart.

The service steps are `claim` (`claim-batch`; on a refused key only the epochs this attempt
was granted are released, each at its epoch, and a failed release is reported), `ci_class`
(runs of the head that belong to the pull request; green when every required workflow ran
and nothing failed, red naming the failing run, an error when the runs do not finish within
the waiting budget) and `train_join` (the `--train-cmd`, called with `--head --pr --run
--attempt`, printing `{"train":..,"manifest":..}`).
