# L2 brief: build-L2-rt-side — 4 small lanes beside the critical path (parent build-L1; TTL 6 h)
Rules: l2-rules-common.md (binding; sent with this brief). Sonnet, effort medium. HOST workstation. Own worktree plangraph-wt-rt-side.
Budget D-1919: writers qwen3.8-flash via qodercli first, then D-1704 order; seats non-Claude, 2 families that differ from the writer
(qwen writer -> gemini agy + mimo); you orchestrate only (<= ~10 lines yourself); delegate all reading. Never merge, never live steps,
never write the live keeper. build-L2-rtp1-predeploy-4 owns KEEPER-WORKER-VERBS + DEVSTACK + E2E: never touch tools/devstack.sh
except in lane D after predeploy-4's DEVSTACK follow-up merged (rebase on it).

D. plangraph:DEV-PGDRILL-LOCAL (operator D-1955; dep DEVSTACK): (a) tools/devstack.sh host-agnostic (no workstation ports/paths; temporal
   CLI fetched + pinned + sha256-checked if missing) so coding.vm runs it unchanged; (b) tools/pg-drill-local.sh: pg_dump -> restore into
   throwaway postgres:16 -> compare_counts -> refuse vacuous restore (0 tables or 0 rows = fail) -> stdout `PG-DRILL ok rto_s= rows=`
   + counts; hermetic test (fake docker/pg on PATH); 2 seats. build-L1 does the real coding.vm run after merge.
G. PG-PUBLIC-READY gitleaks allowlist: build-L1's scan (gitleaks 8.30.1, main .gitleaks.toml, all 173 public branches) = 1 finding:
   generic-api-key rust/worker-core/src/sandbox_env.rs:312 (test sentinel for OPENCODE_API_KEY, judged fake). .gitleaks.toml allowlist
   entry path + regex + reason; tiny PR, trailer `Plan-Item: plangraph:PG-PUBLIC-READY`; 1 seat.
P. PG-PUBLIC-READY review (operator D-1956; HIGH, in_review since 10-17): 2 non-Claude seats against the item's done text
   (LICENSE/CONTRIBUTING; 5 history findings triaged; site strings out; D-NNN policy; deploy/ site configs and plan data out; CI without
   secrets; names-list scan clean). ONE seat (tool-using) runs gitleaks itself on a fresh `git clone --mirror` of the public repo
   (all refs) and attaches the report. Seat dirs review.txt + evidence.json -> MERGE-READY HIGH to build-L1 (judge).
E. plangraph-work (private GitHub repo) archive triage (D-1751): table per ref: lane/{le-guard-key, merge-kg-ui, merge-kg-ui2,
   pg-le-guard-key, qa-p3-answer-ui, rf-p4-archive-answers, rf-p4-archive-answers-r2, rt-p1-inbox-predeploy} + all 32 local-codingvm/*:
   superseded | to-port, reason (git cherry patch-ids vs public main; for squash-merged work compare touched files/functions on main;
   keeper item state). build-L1 pre-check: 13 refs empty vs merge-base; le-guard-key 8 non-cherry commits (~1.1k lines differ on its
   files), qa-p3-answer-ui 3. Table file + sha256; send ETA in your first message.
Per lane: MERGE-READY <pr> <head> + CI + seat dir tar sha256 to build-L1; paste small records (<= 4 KB) inline (Taildrop unreliable).
BLOCKED <item> | what | options, *default. RECYCLE-READY <card> before 230k.
