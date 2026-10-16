# build-L2-live-sync-gov card
state: DONE, MERGE-READY HIGH 7c1ef8be, PR #38 (draft), branch lane/live-sync-3fd91bd6 (docs only)
packet: docs/plan/rethink/live-sync-3fd91bd6.md ; log: docs/plan/rethink/live-sync-3fd91bd6.rehearsal-log.txt
seats: /home/s/code/plangraph-worktrees/live-sync-gov-seats/{openai,qwen} (gpt-oss-120b pass, Qwen3.8-Flash pass, 2nd try)
rehearsal: PG17 container (live is 17.11, brief said 16), removed. migrate 4 ok, set_metrics keeper_rt 200, worker 403, b3d71096 starts+writes on schema 4.
findings: default privileges already give keeper_rt S/I/U/D on metrics (GRANT redundant, kept); optional REVOKE DELETE; no sequences.
live steps: none run. build-L1 runs packet sec 2 after GO.
