# L2 brief: build-L2-temporal-tls — plangraph:RT-P1-TEMPORAL-SPIKE-TLS (prio 1; gates server:RT-P1-TEMPORAL-CHECK; WORKSTATION; parent build-L1)
Rules: /home/s/code/plangraph-worktrees/build-L1/docs/plan/rethink/briefs/l2-rules-common.md (binding, incl. D-1380 local-CI fallback, PR-head trailer). TTL 3 h.
Done means (keeper item): the spike client connects with client cert + key + CA to an mTLS Temporal frontend (Server repo
lane/rt-p1-temporal 8640037ab, nginx sidecar — read its nginx/TLS config for SNI/server-name and CA expectations); flag/env documented
(e.g. TEMPORAL_TLS_CERT / _KEY / _CA / _SERVER_NAME; plain connect unchanged when unset); replay test still green.
Scope (build-L1 decision): the production worker has the SAME plain connect — rust/worker/src/bin/worker.rs:58, spike
src/bin/{worker,driver}.rs:16/201. Put one small TLS-options helper in each crate (or shared if trivial) and wire all three binaries,
so RT-P1-WORKER deploy is not blocked by the same gap. Tests: unit test that env -> ConnectionOptions tls config (missing file = clear
error, partial set = refusal); an mTLS round trip against a local Temporal dev server behind a throwaway nginx/self-signed CA if cheap
on the workstation (else document the manual check against cloud.vm for TEMPORAL-CHECK, cloud.vm is being started D-1388).
No real certs/keys in the repo or logs (generated test CA only, under tests/ tmp).
Seat: 1 non-Claude seat (gpt-oss via ops/seat.sh or qwen via qodercli) — TLS/auth change, HIGH-style per-vector file:function format.
Worktree from origin/main, branch lane/rt-p1-temporal-spike-tls; draft PR; head trailer `Plan-Item: plangraph:RT-P1-TEMPORAL-SPIKE-TLS`;
CI on workst attempt-1 green (rust-spike + rust-worker jobs), else D-1380 local record; MERGE-READY <sha> + CI run + seat dir (Taildrop + sha256) to build-L1.
