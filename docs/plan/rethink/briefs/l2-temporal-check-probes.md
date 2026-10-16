# L2 brief: build-L2-temporal-probes — server:RT-P1-TEMPORAL-CHECK runbook (b)+(c) (WORKSTATION WSL2 + coding.vm; parent build-L1)
Rules: /home/s/code/plangraph-worktrees/build-L1/docs/plan/rethink/briefs/l2-rules-common.md (binding). TTL 4 h. Effort medium.
GO: D-1513 (routing) covers T6 C0 + T9 B1-B5 probes. NO cloud.vm changes: no deploy/restart/down, no OPNsense/Tailscale/Caddy
(T7/T8/T10 need a separate GO via build-L1). Exception B4 (`scripts/homelab restart server/coding/temporal`) = ask build-L1, prox runs it.
Start only when build-L1 says T3+T4 green and client certs exist (operator OS-208). Certs: presence/perms only, never print contents.
Runbook: Server repo docs/operations/temporal-validation-runbook.md @ improve 314c06d42, sections (b) and (c).
Crate: plangraph origin/main rust/spikes/temporal-spike (TLS env per its README/src/tls.rs, PR SPIKE-TLS). Build on the WORKSTATION
(RAM floor 12 GB free, one CARGO_TARGET_DIR, cargo clean after, D-1319); coding.vm runs only the built binary (floor MemAvailable 1 GB).
Tasks (L3 or direct shell probes; no code changes expected — a code fix = new lane, ask build-L1):
 P1 C0 from WSL2 and coding.vm: tcp 192.168.178.159:22 and :7234 -> case C1/C2/C3 (C2/C3 = STOP, report to build-L1).
 P2 C4 mTLS health SERVING; C5 no cert rejected; C6 wrong-CA cert (throwaway CA) rejected; C7 3-min idle poll no UNAVAILABLE; C8 ask
    build-L1 for the sidecar log line (prox reads logs).
 P3 B1 worker 2 min, B2 driver all scenarios (workflow, activity+heartbeat+retry, signal, durable timer), B3 worker kill mid-timer,
    B5 replay `cargo test --test replay` (3 pass). Same from coding.vm for B1/B2 if the binary runs there.
Record each step `B_RESULT`/`C_RESULT` with first failure text verbatim, host, UTC time; workflow_id/run_id of one spike run (for D4).
Output: report file /home/s/code/plangraph-worktrees/build-L1/seats-archive/temporal-check/probes.md (<= 80 lines, no hosts beyond
the runbook's, no secrets) + DONE line to build-L1. Never merge, never write 127.0.0.1:8787.
