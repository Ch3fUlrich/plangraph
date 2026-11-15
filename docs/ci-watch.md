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
Concurrent writers are refused by a file lock. State replacement is atomic.
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
