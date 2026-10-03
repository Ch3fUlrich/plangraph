# Status card — plangraph-L1-main (Opus 5.5 high, L1 project orchestrator; relaunched fresh 2026-10-01 ~20:15)
Updated: 2026-10-02 23:40 UTC | Brief: routing/docs/plan/briefs/plangraph-L1-main.md | Constraints: CONSTRAINTS.md | Handoff: docs/HANDOFF.md

## Phase
M1 CLOSED. M2a code accepted (routing-00 Opus spot review). Now: X6 fixes -> X7 tiers -> 2-seat re-review -> pilot keeper -> plan-v3 load ->
router batch -> render --cdn -> M2c research + proposal (D-231). M2b / M3 / M4 not started.

## State (branch worktree-proposal, worktree .claude/worktrees/proposal)
- PILOT KEEPER LIVE (coding.vm 127.0.0.1:8787, "isolation not effective (pilot)"; stop by PID file, never pkill -f; daemon does NOT write
  its pid file: write it after start). Daemon on 610271d code since 19:00Z (MODEL-A + integ + mask + tier-edit); descriptions migrated.
- Merged + accepted: X7 (D-312), apply_ops+B3 (D-371), md-sync (D-388, timer ~/.config/systemd/user/plangraph-md-sync.*, halts on
  refusal), B2..B2d (D-400), MODEL-A (D-427, request-side seats; re-seat lane plangraph:RESEAT-MODEL-A after autoos:EVTAG).
- Applied batches: data/updates/* (QUEUE.md), router closes D-395/D-413/D-422, autoos WS-SHIFT, RECONCILE (86), MCP-ONE.
- IN FLIGHT (workstation via workst-L1, Taildrop; D-389/D-430: runs only via autoos-agent spawn):
  * integ fix DONE cfda342 (968 green); mask port writer queued (pkg b38b82ec); then ONE seat pair (integ+mask+tieredit) -> merge, restart daemon, `pg migrate-descriptions`, rehome batch
    (10 open workstation:* -> T5-* autoos host=workstation, OP-* served project; D-413).
  * tier-edit keeper fix (pkg writer3 59785f94, D-434) -> seats -> merge -> D-428 closes (T3-CIGREEN/T3-CIMAIN).
  * md-sync mask-on-import (pkg writer2 bd685d62, D-405) -> rebase after integration fix.
  * UI-BUILD (L2 plangraph-L2-ui-build): T1 MiMo writer (pkg f0c7604a); T2-T4 Gemini via OmniRoute pin (D-424) when autoos-L1 proves it.
  * SPLIT-PREP DONE (L2-DONE): tooling merged fcc1839..30d2419 (+ splitinteg; 1028 green WSL; worktree scan "check ok 162 files"). Real-repo steps open.
- Rules in force: template docs/process/L2-BRIEF-TEMPLATE.md (D-430/431 spawn-only, D-389 placement, D-349, D-302 independence,
  D-421 MiMo normal, quote+VERDICT rule). L2s report only to me; I report to routing; workst-L1 direct (D-319); 2-h progress line (D-418).
- OS-92 GRANTED (operator, this session): L1 may write router-approved batches (closes, rehome, priority) AND router-OK project plan batches (create/edit/priority) under data/updates/ and apply them to :8787 with apply_batch, then verify.
- INCIDENT 2026-10-02 ~23:16Z (closed for tonight by routing): workst-L1 agy run 20261002-231606-t2c-d813a6 saw a non-clean ui-build tree (fleet project names in test_render.py, file-name list, workstation username); LOW, real breach; sandbox kept for audit until the operator has seen the morning report. T2b guard widening reverted by T2c (MiMo).
- WORKST ACK RULE (fleet): every task to workst-L1 gets RECEIVED → LAUNCHED <run-id> | QUEUED pos N within 10 min; no ack within 15 min → ping workst-L1.
- PATH 1 (md-sync D-row Closes) GO by routing: on merge FIRST stop plangraph-md-sync.timer+.path, run a DRY RUN, send routing the (key, D-row) list, apply only after routing OKs/strikes keys, then re-arm. Later runs need no OK.
- DESIGN TODO (routing audit 2026-10-02): item kind `decision` (or linked D-row import, ~130 rule answers) + `question` kind -> M2c model proposal with MODEL-KINDS.
- Lessons: quoted heredocs for task texts; legacy-claim only items with a known close path; Taildrop (D-474): NEVER `tailscale file get`; read only /home/s/fleet/taildrop/plangraph/ (prefix stripped; dup -> x.1); senders keep the plangraph__ prefix; check the file is FILED (ls + sha) before pointing an L2 at it.
## Compactions this session: 1 (2026-10-02 ~17:55Z)
