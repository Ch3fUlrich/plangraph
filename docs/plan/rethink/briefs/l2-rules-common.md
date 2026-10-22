# Common rules for build-L1 L2s (bind the L2 and every L3; copy into each L3 brief)
- Load skill `unattended-orchestration` first. L2 = Sonnet orchestrator: split, brief, spawn, review; never write code itself, never merge
  to main, never do live steps. Parent = build-L1 (SendMessage `build-L1`). Report <= 30 lines at phase end, then stop.
- D-437/D-1317 (binding): on CODING.VM only tool-less writers/seats (review-call: L2 applies the returned diff); tool-using opencode L3s (autoos-agent spawn) only on the workstation. Commit trailer adds `Writer: <model> <run id> [tool-less]`.
- WRITERS = D-2148 (operator 2026-10-22, replaces D-1704 for implementations): cx/gpt-6.1-sol (Codex via OmniRoute) first; NEVER Claude Sonnet
  for implementations (tiny fixes included); never cx/gpt-5.6-sol. coding.vm OmniRoute does not serve 6.1 yet -> mimo/mimo-v2.6-flash
  (tool-less review-call, gateway id xiaomi-mimo/mimo-v2.6-flash, as ops/seat.sh), then omniroute/deepseek-direct-flash; record each skip + reason.
  opencode go+free rate-limited: skip. One closed task per writer brief (one file/module, exact spec, file:line anchors; R-orch-02); gate every write:
  diff stat, tests, files-read; relaunch, never resume, a no-change run. Writers in parallel only with disjoint write sets.
- SEATS = D-2166 order: opencode free > openrouter free > qodercli qwen3.8-flash > agy gemini-3.8-flash > mimo-v2.6-flash > agy claude-sonnet-5.5-medium
  > deepseek-v4-flash (last resort). Family differs from the writer and each other; skip a rate-limited leg + record why (2026-10-22: opencode 429,
  agy gemini quota 429 ~33 h, openrouter free has no direct spawner path). agy sonnet is never the non-Claude seat of a HIGH lane.
  - SEATS (D-2205, amends D-2148/D-2166): first available whose family != writer (and != each other): 1 opencode free glm-5.3-flash-free / deepseek-v4.1-flash-free > 2 OmniRoute free: openrouter free (laguna, north-mini), NIM free where served > 3 qodercli qwen3.8-flash > 4 agy gemini-3.8-flash (429 until ~10-23 22Z) > 5 mimo/mimo-v2.6-flash (prefixed id, never a combo; prefer the server gateway) > 6 Claude Haiku 5.5 or agy claude-sonnet-5-5-medium (Claude: max ONE per lane, only over a non-Claude writer, never the non-Claude seat of a HIGH lane) > 7 deepseek-v4-flash paid (last resort). HIGH = 2 seats of distinct families, each != writer. Skip rate-limited legs with reason. READY seats stay valid. Seat record = served model + provider + family + verdict.
  - SEAT FAMILY (D-2217, operator, amends D-2205): family judged PER WRITER (Writer trailer) of the commits reviewed, not lane-wide; in a mixed PR every commit needs the tier seat count from families foreign to its writer (gemini may seat qwen commits, qwen may seat gemini/mimo commits). Claude Sonnet 5.5 / Haiku 5.5 may seat over non-Claude writers; 2 Claude seats = 1 family. HIGH = 2 distinct families. Auth/secrets/live lanes keep >=1 non-Claude seat. gpt-6-luna (OpenAI) may seat over mimo/qwen/deepseek/gemini/glm code, never over cx/gpt-6.1-sol code.
  - HAND-EDIT (D-2240, judge): an L2 hand edit goes in its OWN commit with trailer `Hand-Edit: <reason>`, never folded into a writer commit; auth/door-surface edits (verb registration, roles, scopes) go to a writer, never by hand.
  - WRITERS NOW (D-2248, until server OmniRoute update): NO codex/cx on coding.vm/server gateway; order opencode free > mimo (prefixed) > deepseek; record skips. Operator away to ~2026-10-23T05:00Z: questions to build-L1/router only.
  (older) D-2195: while OR free fails, deepseek-v4-flash takes its slot (2nd); mimo seats no-tools only; agy gemini 429 until ~10-23 22Z.
  Seat records: served model + provider + family. Spawner first (D-2038); a leg the spawner does not declare -> ops/seat.sh (gateway review-call).
- Authorship D-2046/2047: every NEW commit carries `Writer: <served model> <run id>`; ci-gate refuses missing/Claude writers; existing commits never redone;
  removing a test needs `Test-Removal: D-NNNN`.
- RAM floors (operator 2026-10-15): coding.vm MemAvailable >= 1 GB, workstation >= 12 GB free. Heavy Rust/Temporal builds on the
  workstation, one CARGO_TARGET_DIR, `cargo clean` after (D-1319).
- Seats (tool-less, max 4 fleet-wide): qwen =
  qodercli in an empty dir: `timeout 900 qodercli -p --model Qwen3.8-Flash --tools "" --no-session-persistence -o json "$(cat PROMPT)"`
  with the first line "You have NO tools and no repository access. The full material is inline below. Answer in plain text; never
  emit tool calls." NEVER ovh Qwen (times out). Prompt = vector block V1..Vn + diff + ALWAYS /home/s/code/plangraph-worktrees/build-L1/ops/tail.md last.
  Split diffs > ~60 KB by module with a file->part map. Adjudicate each finding against code. Do not record keeper verdicts; return seat dirs.
- Judge notes (PR #60, 2026-10-17):
  - A seat must give findings or reasons per vector; a bare "PASS" is not a verdict.
  - Ask the seat to verify each claim (scan results, test counts); never state a claim as fact in the prompt (D-1607).
  - Seat prompts can embed removed site strings: keep them private (never push them to the public repo); give the judge path + sha256 + diff-coverage count.
  HIGH lanes: 2 non-Claude families PASS on the final head.
- Tests (operator 2026-10-16 ~03:4xZ overrides D-1338 for small changes): small change (docs/briefs, <= ~50 changed lines, one module, no schema/auth/migration/HIGH path) = run only the affected tests (`pytest -q <touched test files>` / `cargo test -p <crate>`), no full suite; docs-only PRs need no CI. Larger or HIGH changes: full local suite once before the PR push, then CI (PR into main only). Never push a red lane.
- Every commit ends with ONE trailer paragraph (no blank line inside): `Plan-Item: <key>` / `Co-Authored-By: ...`.
  Check `git log -1 --format='%(trailers:key=Plan-Item,valueonly)'`.
- Public-repo hygiene: no hosts, IPs other than 127.0.0.1, home paths, user names, token-shaped strings;
  `python3 -m pytest -q tests/test_split_make_public.py` green.
- NEVER touch /home/s/code/plangraph/.claude/worktrees/proposal or write to 127.0.0.1:8787. No systemd changes on coding.vm; docker only for a throwaway test DB container (--memory 1g, removed at end; D-1569), never for agent runs (D-437).
- Denials verbatim + stop that step. Blocked: one line `BLOCKED <item> | <what> | options, *default` to build-L1, keep working.
  Decide best-practice questions yourself (D-935). Output D-658 caveman.
- Messaging + beats (D-1364, binding): message build-L1 only for BLOCKED/decision needed, DONE/MERGE-READY with sha, GO requests,
  incidents, launch/recycle requests. No acks, "noted", ALIVE, status relays or progress chatter; at most one batched message per beat.
  L2s run NO beat cron; wait on messages and your own tool results.
- PR head trailer (D-935, build-L1 gen-5 2026-10-16): CI runs only on the PR head, so the head commit (incl. a "Merge origin/main" commit) must carry `Plan-Item: plangraph:<KEY>` (full key). Else commit+ci evidence cannot verify (INBOX 1cb7acc, WORKER bff8725).
- D-1380 (operator, prio 1): CI never waits for a GitHub reset. If GitHub CI does not start within 10 min or fails for infra (quota/billing/runner offline), run the repo CI command locally (workstation, or coding.vm within the 1 GB floor) and record by sha: sha, command, exit, log path, host -> report it in MERGE-READY and proceed. plangraph CI = `pytest -n auto` (SQLite) + pytest-postgres job (tests.yml) + rust/rust-worker (cargo test; Temporal SDK builds on workstation).
- D-1390 (operator 2026-10-16): all L1/L2 Claude sessions run at effort MEDIUM; every L2-LAUNCH says "sonnet medium" (workstation/prox: --effort medium). CI = GitHub-hosted ubuntu-24.04 since #59 (public repo); lanes cut before #59 must merge origin/main to get it. Local record per D-1380 if GitHub CI is down.
- D-1984 (operator, Q19): before asking anyone what to do, read the plan item(s) concerned (description, done_criteria, deps, evidence, decision text; the brief carries them, build-L1 relays live keeper reads) and the design/docs/plan files they name. Ask only what the plan leaves open, and name what you checked.
