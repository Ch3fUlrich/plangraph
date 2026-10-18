# L2 brief: build-L2-rtp1-predeploy-3 — successor of predeploy-2: #49 INBOX finish + RT-P1-DEVSTACK (parent build-L1; TTL 6 h)
Rules: /home/s/code/plangraph-worktrees/build-L1/docs/plan/rethink/briefs/l2-rules-common.md (binding). Sonnet, effort medium. HOST workstation.
FIRST read your predecessor's card C:\fleet\handoff\build-L2-rtp1-predeploy-2.card.md (state, seat dirs, worktrees). Stand predecessor down (R-orch-28).
Budget D-1919: writers qwen3.8-flash via qodercli first, then D-1704 order; seats non-Claude, 2 families; you orchestrate only (<= ~10 lines yourself).
Operator D-1932: only PH-RT-RUNTIME. Never merge, never live steps, never write the live keeper (127.0.0.1:8787 read-only via build-L1).

## Lane A — #49 INBOX (plangraph:RT-P1-INBOX, standard risk), head 0d05f76, CI green 39111017886/905/929
Fix round: gemini LOW (staged file not recorded when copy fails mid-way -> record before copy / clean on error), qwen MED (pre-existing staged
sub-dir not forced to 0700). Re-seat that delta with 2 non-Claude families (qwen must end with a VERDICT line; rerun if missing).
MERGE-READY #49 <head> + CI run + seat dir tar sha256 (Taildrop to coding-vm).

## Lane B — plangraph:RT-P1-DEVSTACK (claimed by build-L1, epoch 1; D-1943), parallel to A
Branch lane/rt-p1-devstack from origin/main, head trailer `Plan-Item: plangraph:RT-P1-DEVSTACK`.
Done: tools/devstack.sh start|stop|status:
1. upstream `temporal server start-dev --ip 127.0.0.1 --db-filename <state>/temporal.db`, frontend/UI ports via env (default 7233/8233;
   workstation uses 17252/18235), plus `temporal worker deployment set-current-version` (without it the driver hangs);
2. local Postgres 16 for the keeper: ONE `docker run` of official postgres:16 (as CI pytest-postgres: trust auth, DB keeper, 127.0.0.1 port);
   *pg16 initdb/pg_ctl if docker absent. NO compose file, no dynamicconfig, no custom temporal image. Parked #71 stays parked (reuse only fitting bits).
3. keeper runs on it (PG_KEEPER_BACKEND=postgres, PG_URL); 4. pytest-postgres green against it (command + 'N passed' line).
Hermetic shell test (fake temporal/docker on PATH, shellcheck-clean). 2 non-Claude seats. MERGE-READY #<pr> <head> + run record
(start/stop/status output, pytest-postgres line) Taildropped with sha256.

Messages to build-L1 only: MERGE-READY / BLOCKED <item> | what | options, *default / DONE. Never wait idle. RECYCLE-READY <card> before 230k.
