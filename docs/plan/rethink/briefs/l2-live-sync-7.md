# L2 brief: build-L2-live-sync-7 — guard follow-up fix, then batched live-sync packet keeper 102e4885 -> main T (HIGH; parent build-L1; TTL 5 h)
Rules: /home/s/code/plangraph-worktrees/build-L1/docs/plan/rethink/briefs/l2-rules-common.md (binding). Never merge, no crons, effort medium.
HOST coding.vm: D-437/D-1554 ONLY TOOL-LESS writers/seats (recipe: /home/s/code/plangraph-worktrees/keeper-verbs-seats/L2-keeper-verbs-card.md).
D-1504: qwen3.8-flash only via qodercli; mimo via mimo provider; gpt-oss via ovh. D-1272: L2 <= ~10 lines itself. Own worktrees under plangraph-worktrees/.
NO LIVE STEP: never restart/stop plangraph-keeper.service, never touch live PG schema `keeper`, never write 127.0.0.1:8787. build-L1 runs live steps after GO (D-825).
PART 1 (HIGH, secrets guard) plangraph:PG-GUARD-SEP-SPLIT (claimed by build-L1; done text `ops/pg.sh show plangraph:PG-GUARD-SEP-SPLIT` from build-L1):
 judge follow-up on #55: 'login admin/hunter2secret,docs/x/y.md' -> 0 shapes (one \S+ token, path-like). Split tokens on [,;] (and similar) BEFORE the
 path exemption in keeper/credential_guard.find_shapes; test the bypass refused, MT-003/MT-095 still pass, the D-1613 gap test unchanged. Branch from main,
 draft PR, trailer Plan-Item: plangraph:PG-GUARD-SEP-SPLIT, CI, 2 fresh non-Claude seats (different families) on the full diff, SAVE each seat's prompt.txt.
 MERGE-READY HIGH <sha> to build-L1, then wait for build-L1's "MERGED <main sha>" before part 2.
PART 2 packet (template: docs/plan/rethink/live-sync-102e4885.md on main; copy its structure): live now 102e4885 (LIVE-SYNC-6, drop-in live-sync-z6-102e4885.conf,
 record build-L1 seats-archive/live-sync-6-102e4885/LIVE-RECORD.md). T = main sha build-L1 gives you after part 1. Batch: UI-TABLE (#52,#54), PG-NEEDS-YOU (#53),
 PG-UI-TABICON (#50), PG-GUARD-PATH-EXEMPT (#55), PG-GUARD-SEP-SPLIT, plus anything else on main since 102e4885 (list it all from git log).
 Verify claim "code-deploy only": diff 102e4885..T on schema_pg/db_pg/config/migrations, roles/grants, MCP tool list, new event types. Drop-in live-sync-z7-<T8>.conf.
 Rehearsal: throwaway postgres:17 container (--memory 1g, random port, removed at end; D-1569 allows it) on a fresh read-only dump; T daemon as keeper_rt on scratch
 port/token: verify + verify-snapshot, snapshot 200, rendered page served (UI changes) and guard behaviour; rollback to 102e4885 proven on the T-written copy.
 State the roll-forward rule: ROLL FORWARD ONLY once T writes anything 102e4885 cannot fold (prove whether any such write exists; if none, rollback = drop-in removal).
 Branch lane/live-sync-7 docs only, draft PR, trailer Plan-Item: plangraph:PG-GUARD-SEP-SPLIT. 2 non-Claude seats (no-migration claim, rollback validity, secrets hygiene).
 MERGE-READY HIGH <sha> + PR + rehearsal summary + seat dirs to build-L1. RECYCLE-READY <card> before 230k.
