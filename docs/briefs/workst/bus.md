# L2 brief: build-L2-bus   (lane F)
## Lanes, in order:
1. plangraph:RT-P1-BUS-CONTRACT (HIGH): new branch lane/rt-p1-bus-contract off origin/main. Read AutoOS tools/fleet_bus.py (anchors in desc) from a read-only AutoOS checkout on the workstation.
2. plangraph:RT-P1-BUS-KEEPER (keeper Postgres, HIGH) after BUS-CONTRACT is MERGE-READY (build-L1 claims it then). Body text in keeper, never in the metadata jsonl (D-2024).
3. Low prio (1): plangraph:KEEPER-BATCH-PRECONDITION PR #97 (lane/keeper-batch-precondition, head 7c787302): finish per its PR body; keeper engine = HIGH (2 seats + judge GO-REQ).
HIGH lanes: GO-REQ to build-L1 with branch, red-first head (tests only) and red run before the implementation writer starts.
