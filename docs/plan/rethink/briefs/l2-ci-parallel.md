# Phase brief: plangraph:CI-PARALLEL (follow-on phase for build-L2-backup after PG-BACKUP-RETENTION; parent build-L1)

Same rules block as /home/s/code/plangraph-worktrees/build-L1/docs/plan/rethink/briefs/l2-backup-retention.md (L3 free first, in-session
Sonnet writer if spawner admission refuses, D-1338 local suite green before push, trailer `Plan-Item: plangraph:CI-PARALLEL`, public
hygiene, never touch the live keeper, denials verbatim, D-658, report <= 30 lines to build-L1). Seats: openai via ovh/gpt-oss-120b
(ops/seat.sh) or qwen via qodercli (`timeout 900 qodercli -p --model Qwen3.8-Flash --tools "" --no-session-persistence -o json
"$(cat PROMPT)"` in an empty dir; prepend "You have NO tools ... answer in plain text"); never ovh Qwen. Append ops/tail.md last.

## Done means
tests.yml runs pytest with `-n auto` (pytest-xdist) and cached pip deps (both pytest and pytest-postgres jobs); tests that share
state (fixed ports, cwd, global git config, env, temp names) made xdist-safe without weakening asserts; slow waits found with
`--durations=30` and fixed or bounded (sleeps -> events/short polls, network timeouts -> stubs, subprocess startup -> shared fixture);
local suite on coding.vm far below the measured 960 s wall (6% CPU, 44 s user); CI green; 1 seat.

## Facts
- Main CI 3f353504: pytest job 3m43s, `2488 passed, 1 skipped in 213.24s` serial on GitHub-hosted 2 vCPU.
- Local (coding.vm, worktree from origin/main): `2489 passed in 959.97s`, `43.99s user 17.18s system 6% cpu`.
- Worktree: /home/s/code/plangraph-worktrees/ci-parallel from origin/main, branch lane/ci-parallel, draft PR for CI.
- Do not change `runs-on` (that is plangraph:CI-RUNS-ON-WORKST after the workstation runner exists).
