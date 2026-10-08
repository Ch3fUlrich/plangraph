# T4 — The opencode fleet: harden, certify, and use as the automatic fallback

Owner: AutoOS L1-main (container/config lanes) + routing-00 (certification runs and the fallback drill).
Role (operator 2026-10-01): production runs on Claude L0/L1; the full L0–L3 opencode fleet is the **certified fallback**
that takes over automatically when Claude limits are hit.

| Lane | What | DONE (evidence) |
|---|---|---|
| T4-HARDEN | compose: `init: true`, `pids_limit: 32768`, `mem_limit` + memswap set in compose (not `docker update`); opencode tracks the latest v2 release (no fixed pin): weekly update job builds, smoke-tests (spawn + compaction + tool call), auto-rolls back on failure; snapshot git off or throttled for large trees (39k of 49k spawns were snapshot git) | recreate → Init=true; pids < 200 after 24 h of fleet load; update job log with smoke + rollback test |
| T4-HOST | Heartbeat and auto-resume on the HOST (systemd timer → `docker exec … router-heartbeat.sh --once`); after any container start, sessions with an open turn are resumed; timestamps in real UTC; delivery check on every wake (no duplicates on aborted POSTs) | kill the container mid-lane → all orchestrators resume within 10 min, no duplicate orders |
| T4-FENCE | Permission rules rewritten: verb-anchored patterns (no substring hits like `git merge-base`), secret deny covers `api-keys*`/`api_keys*`/`*.key`/`*.pem`/credentials JSON; secrets outside the mounted tree where possible; **orchestrators** may run git merge/reset/branch/worktree and push lane branches (main only via take); **workers/reviewers** may not push, merge, reset or read secrets; the push parser (HOOKS H2, D-159) as the real gate | fence test corpus: every past false positive allowed, every past bypass denied; canary (T2-CANARY) never appears in transcripts |
| T4-MSG | Inter-agent messaging that works: order/ack/reply via inbox lines with ids + `fleet-wake` with delivery verification; children can report to their parent; a stuck child is visible to the parent within one beat | test: 3-level message round trip under load |
| T4-ZEN | opencode config ships the Zen provider with our key (T1-ZEN) and explicit model `limit.*` from the registry | spawn on a Zen free model OK; no compaction before 65 % of the real window |
| T4-CERT | **Certification run**: router + L1-main + 2 L1/L2 tracks + L3 workers/reviewers, all inside opencode, on the trial/free tiers, executing the certification scenario set (TESTING.md §4) for 12 h; the routing session uses `AGENTS.md` | scorecard: all scenarios pass, 0 silent stops, 0 unrecovered crashes, ≥ 2 families per review, 0 paid calls except Sonnet finals; run twice on different days |
| T4-SWITCH | Fallback switch built as a NON-agent unit (systemd on the host; agents may not launch harness sessions, D-158): when the Claude limit error is detected for L0/L1, it starts the opencode L0/L1 from the same cards and parks the Claude sessions; switches back after the reset | drill: simulated limit → opencode fleet takes over within 15 min and continues the same lane; switch-back drill |

Order: T4-HARDEN → T4-HOST → T4-FENCE → T4-MSG → T4-ZEN → T4-CERT ×2 → T4-SWITCH (+ drill).
The workstation runs the same certification against its own opencode (T5) — "must still work on the workstation".
