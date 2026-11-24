# CI watch

Run `plangraph ci-watch` from a plangraph checkout with authenticated `gh` on PATH.
It scans completed `tests.yml` runs, paginates open PRs and jobs, reads one page
(100 newest) of runs per main branch or open PR head, and takes only the newest completed run per open PR head/event or main branch/event.
PR runs must match the current open PR head SHA. Successful newest runs suppress
older failures. Closed PRs are ignored. The repository comes from origin.
Run queries are capped at one page (newest first), so API use is one call per
open PR head plus one for main and does not grow with run history.

`--dry-run` prints proposed actions without writing state, rerunning jobs or
calling keeper. `--keeper` creates priority 10 lanes under
`plangraph:PH-ORCH-SETUP` using the keeper CLI's `PG_URL` and `PG_TOKEN_FILE`.
The CLI loads `~/.config/plangraph/plangraph.env` (or `PLANGRAPH_ENV_FILE`);
process environment overrides file values. The token must be allowed to create
lanes in plangraph and attach them to the active PH-ORCH-SETUP parent. A missing
or terminal parent is reported; ci-watch never changes that parent. Keeper
uses its project default review tier. Keeper outages leave tasks locally
for the next invocation to retry; delivery errors never stop other local tasks.
Unavailable logs/jobs produce fallback tasks, and refused reruns escalate to tasks.
Delivery failures return exit 2 after all local tasks are saved and printed.
Definitions have Why:/How: descriptions bounded to keeper's 200/2000-character
title and description limits and credential guard. Keeper receives bounded,
scrubbed copies; the full versions of test IDs and excerpts stay local.

State is in `~/.local/state/plangraph/ci-watch/`: `tasks.jsonl` holds one record
per repository/head/test, updated by later failures; `reruns.json` records rerun
reservations and newest completed selections (so an in-flight rerun cannot
revive older completed failures); `recurrences.jsonl` records known flake sightings and keeper keys.
Concurrent writers are refused by a file lock (a busy lock is retried for up to 2 s first, so a fork that briefly holds the fd does not flake it). State replacement is atomic.
An active keeper lane is reused on later runs. A regression after that lane
closes gets a deterministic run-suffixed successor; `keeper_key` tracks that
lane while the local head/test row stays unique. A closed lane for the same run
is already handled. Recurrence sightings are deduplicated by run/attempt/action.
A rerun reservation is written before the request to prevent repeat requests
if GitHub's response is lost; inspect GitHub before manually removing one.

`deploy/ci-watch/flakes.toml` maps exact pytest/Rust test IDs to keeper items.
Known flakes get one failed-job rerun per run ID; a GitHub attempt greater than
one also counts as already rerun. Further sightings produce fix tasks and
recurrence records. Other failures on a mixed run get tasks immediately.
Unparsed build/job failures get a `<job-failure>` task rather than disappearing.
Job logs are streamed with a 1 MiB read cap (plus one sentinel byte); excerpts
contain at most 40 lines. Provider tokens, JWTs, private key blocks, secret
assignments, bearer tokens,
credential URLs and sensitive query parameters are scrubbed before persistence
or printing. Raw logs are never stored. Failures beyond the cap may be represented
by a job fallback instead of their test IDs.

The supplied user service/timer run every five minutes with `--keeper`.
Adjust `WorkingDirectory` and `ExecStart` for the host, then install/enable them
explicitly as user units. This change does not install units. `gh`, the built
Rust CLI, and keeper credentials must be available to the user service.

## Main-freeze handler and batch failures

`plangraph ci-watch --keeper --main-freeze` (the supplied `pg-ci-watch` service runs exactly this; without `--keeper` fix tasks stay local and are not defined or cancelled in the keeper) acts on the runs `plangraph main-state` names as blocking (docs/ci-parallel.md).
The recovery table, one action per ref at a time, with a budget persisted per run in `main-freeze.json`:

| Blocking run | Action |
|---|---|
| failure maps to a FLAKE item (`deploy/ci-watch/flakes.toml`), log complete | one failed-job rerun |
| no FLAKE item, a second red attempt, or a capped log | the fix task of rule 3.46 (once per run) |
| cancelled, timed_out, startup_failure, or an expected job missing | one full rerun |
| an attempt older than the bound T | cancel it, then one rerun |
| budget spent, GitHub refuses the request, run id `none` | no action; an alert for routing |

Each request is reserved in the state file before it is sent and reconciled by reading the run's attempts;
after 10 minutes without a new attempt it stays recorded as unresolved, is never sent again, routing is
alerted and the ref is released. The alerts (`main_red_no_progress`, `main_pending_aged`, `main_state_unknown`,
`main_ref_not_forward_only`, `recovery_*`) go to `alerts.json` in the watcher state directory, each with the party
that is due, for the flow watchdog collector to hand to routing.

A failing head of a pull request that `owners.json` (`prs`, `writers`, `successors`) maps to a batch is not turned
into an unassigned fix lane: the failure is recorded first, delivered with its log through
`CI_WATCH_DELIVER_CMD <writer> <record file>` to the batch's current writer (a successor is followed), kept open
until `plangraph ci-watch --ack=<id>`, and escalated to plan-order after 15 minutes. The watcher never starts a writer.

The handler tracks the full runs of main's current tip by run id, whatever ref they ran on and whether or not the train
pull request is still open. It creates the fix task of rule 3.46 when main reads red and cancels it in the keeper when main
reads green again (a failed cancel stays open and is retried on the next pass).

## Red classes

Every red job is classified from its name and scrubbed log (`ci_watch/classify.rs`): fmt, clippy (only lints
`cargo clippy --fix` applies), file-size, missing-trailer, missing-module (a non-Python job importing a module),
flake, code, infrastructure, configuration, unknown; a run whose red jobs differ is mixed. A log that names failing
tests and also an infrastructure or configuration phrase is mixed; a known flake is classed per test, so it never hides a real failure of the same job. The task row, the
keeper definition, the record delivered to a batch's writer and the fix task of a red main carry the job class, the
run class, the failed job id and where the fix starts (`classify::brief_lines`; an infrastructure or configuration
red starts at the runner, workflow or setting, not the batch's code). The class changes the brief, never whether a
task is made.

`decide` gives the first action: a flake is rerun once; fmt and clippy get one automatic fix per pull request and
class; all else goes to a narrow fix writer. `plangraph ci-watch --autofix` (the supplied `pg-ci-watch` service passes it) runs the automatic fix after the
scan (`ci_watch/autofix.rs`), for the failures of that scan only: a scratch clone of the pull request head (a fork
or a moved head is refused), the command run in a sandbox copy under a Landlock jail (`pg-jail`: it reads the system, the toolchain and
the dependency caches and writes only its own checkout and scratch directories; the trusted clone, the watcher's
HOME and credentials are denied by the kernel) with an emptied environment and `CARGO_NET_OFFLINE`, a host that
cannot enforce the jail leaves the fix to the writer, the copy's files staged and checked by the trusted clone (its `.git` is never run), one commit `Hand-Edit: ci-watch <class> fix of failed job <id>` and one plain push. A fix of more than 50
lines, one that touches a path the commit gate protects for Hand-Edit commits (its `AUTH_DOOR_PATHS` and `GATE_PATHS`, held equal by a test) or one that changes nothing is dropped and left to the writer, whose task
stays. Before a fix the run's outcome is read again (a rerun or a concurrent scan may have made it green). The commit and the push run in the trusted clone with whatever hooks the operator configured for git: the clone's own repository hooks are not installed in a fresh clone, and the repository's pre-push gate would build the pull request's code outside the jail. The (repository, head SHA, class) is reserved in `autofix.json` under a lock before the fix runs, with the job id
and result, and the head a fix produced is entered too, so a fix that stays red is never fixed again while a later
head of a person gets its own. A closed or merged pull request is read once, recorded in the same file and never touched again.

`plangraph ci-watch --report` prints the coverage of `red-runs.jsonl` (one row per red run attempt, written by the
scan, with its jobs and classes): red runs per head commit (`pr-N@sha`) by class, red runs without a class or
without a task (a known flake awaiting its rerun is not one) and the automatic fixes by class (pushed or left to the writer). The scan prints one
`red:` line per run the first time it sees it.
