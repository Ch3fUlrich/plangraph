---
name: plangraph-spawn
description: Dispatch per-host agents using plangraph cards.
---

Brief order: use the versioned `src/plangraph/review/{writer,review}_brief.md`
templates via `plangraph.review.prompt.render_brief`. Keep role, rules, Rust language
line, gates and commit trailer format first; put item context and task data last.
A byte-identical fixed prefix lets the prompt cache reuse it across items.

Spawn by card via plangraph-spawn (`python3 tools/plangraph-spawn.py ... spawn --card ...`), never hand-pick a model.
Use `python -m plangraph.spawner.cli route --card @card.json --role implement`.
Then use `spawn --card @card.json --role implement "task prompt"`.
Use the returned `plangraph-spawn wait RUN_ID --timeout SECONDS` to await completion.
Never use `pgrep -f` wait loops: they can match the waiting process itself.
For reviews use `--role review --review-of RUN_ID`; the private writer record
is the source of the family fence. Unknown runs must never run unfenced.
For a seat set never name legs: `spawn --card @card.json --role review
--writer-family F [--writer-family G] --seats "Review"` (MCP `seats: true`)
spawns every seat `route` picks, covering each writer part (docs/SPAWNER.md
"Seats and writers come from route"). Put `diff_ref` {repo, base, head} on the
card; inline-only legs (luna, mimo-pro) get that diff inlined by the spawner.
Banned/off families and paused harnesses live in `planner/route_policy.json`.
Only an actual provider transport response can mark a leg limited.
An exceptional hand pick needs an audited override reason of at least ten characters.

Waits that trigger a later action must use the Rust CLI's detached watcher service:
`plangraph watcher-launch /absolute/job-dir NAME -- /absolute/command ARG...`.
The command owns the entire wait and authorized continuation, including any pending
push or merge; no continuation may exist only in a session child shell. Use a
transient user service, never `systemd-run --scope`, `run_in_background`, or `&`.
The launcher passes the caller's whole login environment to the unit by name (`-E NAME`: PATH with ~/.local/bin, HOME, XDG and opencode config vars), so a detached push finds protoc and opencode serves its models. The launcher returns after service admission and refuses reuse of NAME. It saves
`watchers/NAME/request.toml` before launch; the service writes `result.toml`
atomically plus `stdout.log` and `stderr.log`. Use absolute executable paths and
explicit command arguments/environment (the user manager does not inherit the
session environment). If systemd admission fails, inspect `launch-error.txt` and
the named unit; never silently fall back to a session child or replay a push.

On every orchestrator start or replacement, run `plangraph resume-digest JOB`
for each job directory named by the handoff. Read all completed results and logs,
reconcile pending entries against their recorded systemd unit, and record any
remaining action in the handoff before launching new work. The digest retains
results across reads; `pending` means no result yet, not success or permission to
relaunch. A stopped or failed service without a result requires investigation.
