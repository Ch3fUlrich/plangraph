# Common rules for build-L1 L2s (bind the L2 and every L3; copy into each L3 brief)
- Load skill `unattended-orchestration` first. L2 = Sonnet orchestrator: split, brief, spawn, review; never write code itself, never merge
  to main, never do live steps. Parent = build-L1 (SendMessage `build-L1`). Report <= 30 lines at phase end, then stop.
- L3 writers (operator 2026-10-15 ~19:10Z: cut Claude usage): autoos-agent `spawn` with opencode, leg order
  (1) `omniroute/mimo-v2.6-flash` (MIMO-FIXED 2026-10-15 ~20:5xZ: live OmniRoute combo pins it to mimo/mimo-v2.6-flash, smoke 200 from the mimo provider; a 402 or any OpenRouter id on this leg = incident, report to build-L1, never use openrouter/xiaomi or deepinfra mimo ids), (2) `--free` = opencode/muse-spark-1.3-contributor-free (no key, no gateway),
  (3) OpenRouter free (`omniroute/or-north-mini-code-free`, `or-laguna-s-2.1-free`, `or-nemotron-3-super-free`), (4) qodercli
  Qwen3.8-Flash. Sonnet (in-session Agent tool) ONLY if all of these fail or spawner admission refuses; record why in the lane
  record. One closed task per writer brief (one file/module, exact spec, file:line anchors; R-orch-02); gate every free write:
  diff stat, tests, files-read; relaunch, never resume, a no-change run. Run finished = exit.json / `status`; never poll job.json.
  Several writers in parallel are fine when write sets are disjoint.
- RAM floors (operator 2026-10-15): coding.vm MemAvailable >= 1 GB, workstation >= 12 GB free. Heavy Rust/Temporal builds on the
  workstation, one CARGO_TARGET_DIR, `cargo clean` after (D-1319).
- Seats (tool-less, max 4 fleet-wide): openai = ovh/gpt-oss-120b via /home/s/code/plangraph-worktrees/build-L1/ops/seat.sh; qwen =
  qodercli in an empty dir: `timeout 900 qodercli -p --model Qwen3.8-Flash --tools "" --no-session-persistence -o json "$(cat PROMPT)"`
  with the first line "You have NO tools and no repository access. The full material is inline below. Answer in plain text; never
  emit tool calls." NEVER ovh Qwen (times out). Prompt = vector block V1..Vn + diff + ALWAYS /home/s/code/plangraph-worktrees/build-L1/ops/tail.md last.
  Split diffs > ~60 KB by module with a file->part map. Adjudicate each finding against code. Do not record keeper verdicts; return seat dirs.
  HIGH lanes: 2 non-Claude families PASS on the final head.
- Tests (operator 2026-10-16 ~03:4xZ overrides D-1338 for small changes): small change (docs/briefs, <= ~50 changed lines, one module, no schema/auth/migration/HIGH path) = run only the affected tests (`pytest -q <touched test files>` / `cargo test -p <crate>`), no full suite; docs-only PRs need no CI. Larger or HIGH changes: full local suite once before the PR push, then CI (PR into main only). Never push a red lane.
- Every commit ends with ONE trailer paragraph (no blank line inside): `Plan-Item: <key>` / `Co-Authored-By: ...`.
  Check `git log -1 --format='%(trailers:key=Plan-Item,valueonly)'`.
- Public-repo hygiene: no hosts, IPs other than 127.0.0.1, home paths, user names, token-shaped strings;
  `python3 -m pytest -q tests/test_split_make_public.py` green.
- NEVER touch /home/s/code/plangraph/.claude/worktrees/proposal or write to 127.0.0.1:8787. No docker/systemd on coding.vm.
- Denials verbatim + stop that step. Blocked: one line `BLOCKED <item> | <what> | options, *default` to build-L1, keep working.
  Decide best-practice questions yourself (D-935). Output D-658 caveman.
- Messaging + beats (D-1364, binding): message build-L1 only for BLOCKED/decision needed, DONE/MERGE-READY with sha, GO requests,
  incidents, launch/recycle requests. No acks, "noted", ALIVE, status relays or progress chatter; at most one batched message per beat.
  L2s run NO beat cron; wait on messages and your own tool results.
- PR head trailer (D-935, build-L1 gen-5 2026-10-16): CI runs only on the PR head, so the head commit (incl. a "Merge origin/main" commit) must carry `Plan-Item: plangraph:<KEY>` (full key). Else commit+ci evidence cannot verify (INBOX 1cb7acc, WORKER bff8725).
- D-1380 (operator, prio 1): CI never waits for a GitHub reset. If GitHub CI does not start within 10 min or fails for infra (quota/billing/runner offline), run the repo CI command locally (workstation, or coding.vm within the 1 GB floor) and record by sha: sha, command, exit, log path, host -> report it in MERGE-READY and proceed. plangraph CI = `pytest -n auto` (SQLite) + pytest-postgres job (tests.yml) + rust/rust-worker (cargo test; Temporal SDK builds on workstation).
- D-1390 (operator 2026-10-16): all L1/L2 Claude sessions run at effort MEDIUM; every L2-LAUNCH says "sonnet medium" (workstation/prox: --effort medium). CI default = workstation, ci.vm backup, runs-on [self-hosted, fleet-ci] (switch by fixes-L2-cipool, not lanes).
