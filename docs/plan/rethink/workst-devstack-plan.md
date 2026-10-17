# Workstation dev stack plan (D-1854, build-L1 gen-13, 2026-10-17)
Order: plangraph DEVELOPMENT runs fully on the workstation (WSL2 + docker): dev Temporal + dev keeper Postgres + Rust worker + all tests.
Server (coding.vm keeper, cloud.vm Temporal/PG) changes ONLY on a release GO with a smoke test. Budget mode D-1851: non-Claude writers/seats
(D-1704/1714, go first D-1840). Launches via workst-L1-autoos (D-1358). Nothing here touches a server.

## Dev stack (repo plangraph, new dir deploy/dev/; all ports bound to 127.0.0.1)
| Service | Image / source | Port | Notes |
|---|---|---|---|
| pg | postgres:16 (same major as cloud.vm) | 55432 | DBs: keeper, temporal, temporal_visibility; roles keeper/temporal; init SQL in deploy/dev/pg-init/ |
| temporal-schema | temporalio/admin-tools (version = cloud.vm pin) | - | one-shot, /bin/sh entrypoint (server fix 310c98e2 lesson: image has no bash), setup + update schema both DBs |
| temporal | temporalio/server (cloud.vm pin) | 7233 | depends_on schema completed; dev namespace `plangraph-dev` created by a one-shot; no TLS in dev (TLS = release smoke) |
| temporal-ui | temporalio/ui | 8233 | deep links for runs |
| keeper | plangraph keeper from the working tree (PG backend, schema keeper) | 18787 | run by tools/devstack.sh, not a container (fast edit loop) |
Secrets: none committed; dev passwords generated into deploy/dev/.env (gitignored) by `devstack.sh init`. deploy/dev/dev.env.example documents names.

## Tasks (non-Claude writers; one PR each; MERGE-READY with sha to build-L1; 2 non-Claude seats of distinct families)
D1 deploy/dev/compose.yml + pg-init + dev.env.example + tools/devstack.sh {init,up,down,reset,status,logs} (logs kept on failure: no rm on
   a failed up; lesson T3). Test: tests/test_devstack_static.py (compose parses, ports 127.0.0.1 only, no secrets, pins match a PINS file).
D2 Temporal check on dev (T3 equivalent of server:RT-P1-TEMPORAL-CHECK): idle+load RSS of the stack; Rust SDK workflow+activity+signal+durable
   timer against dev Temporal (rust/spikes/temporal-spike); UI deep link; record in docs/reports/rethink-p1/devstack-check.md.
D3 Keeper on dev PG: `devstack.sh keeper` starts the keeper on 18787 against dev PG; pytest-postgres (PG contract suite) runs against dev PG
   via env (no CI change); record pass counts.
D4 P1 rehearsal = old brief l2-p1-rehearsal-ocgo.md R1-R4 on this stack (WORKER/PROJECTOR/INBOX/REVIEW wiring, then E2E: synthetic card ->
   readiness -> free leg via Temporal + Rust worker -> check -> lock-merge in a scratch repo -> attempt + cost in dev keeper; Inbox answer
   resumes run; reviewer JSON verdict). R1 audit runs now on coding.vm (L3 qwen, run 20261017-193107-p1-audit-qwen-e4c89b) -> feeds D4.
D5 Release path doc (docs/plan/rethink/release-deploy.md): what a production release is (tag + changelog), the ONE server GO (coding.vm keeper
   live-sync + cloud.vm Temporal stack-deploy = old T3 + smoke test: one card end-to-end on prod), rollback per host.
Order: D1 -> D2 + D3 (parallel) -> D4; D5 any time. Max 3 writers.

## Item changes (to plangraph-L1-graphrestructure for the rethink batch)
- NEW plangraph:RT-P1-DEVSTACK (lane, prio 1): D1+D3 done criteria; unblocks RT-P1-TEMPORAL-CHECK.
- server:RT-P1-TEMPORAL-CHECK: retarget done to the workstation dev stack (D2); keep claimant build-L1; deps += RT-P1-DEVSTACK.
- NEW server:RT-P1-RELEASE-DEPLOY (lane, HIGH, live GO): old T3-T6/T9 on cloud.vm + coding.vm keeper live-sync + prod smoke card;
  deps PH-RT-RUNTIME (P1 passes on dev first); the pending T3 #4 GO becomes this item's first step.
- RT-P1-WORKER/PROJECTOR deps unchanged (TEMPORAL-CHECK, now dev). RT-P1-E2E done: "on the workstation dev stack" + "release deploy smoke
  repeats it on prod" moves to RELEASE-DEPLOY.
- plangraph:ORCH-RUNS-WITHOUT-CLAUDE stays the gate for an opencode-go L2 (operator env + go key).
