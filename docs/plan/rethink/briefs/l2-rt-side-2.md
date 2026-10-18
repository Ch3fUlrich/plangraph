# L2 brief: build-L2-rt-side-2 — successor of build-L2-rt-side (parent build-L1; TTL 6 h; sonnet medium)
Rules: l2-rules-common.md + l2-rt-side.md (binding; same budget/model rules). FIRST read C:\fleet\handoff\build-L2-rt-side.card.md
(state, WIP heads, seat dirs). Stand the predecessor down (R-orch-28). Up to 3 writers IN PARALLEL on disjoint files (D-1964); recycle at 200k.
Order: (1) finish #80 DEV-PGDRILL-LOCAL (head 610d0915+, round-2 gemini+mimo seats, real-postgres run) -> MERGE-READY.
(2) Lane Q plangraph:RT-P1-QUESTION-TIMER (HIGH, D-1974; claimed by build-L1 11:01Z, commits after that): workflow waiting on a question
    races the answer signal against a durable Temporal timer at the deadline (rust/worker workflow.rs; deadline_s question.rs:78; keeper.rs:250
    sends the value). Expiry: default answer recorded in the keeper (existing expired/answer verb keeper.rs:96-98) and run resumes; no default
    -> card parks reason question_timeout (/v1/park). Worker restart during the wait keeps the timer (replay test). Tests on the dev stack +
    unit/replay. Branch lane/rt-p1-question-timer, trailer Plan-Item: plangraph:RT-P1-QUESTION-TIMER. 2 seats -> MERGE-READY HIGH.
(3) Lane F plangraph:RT-P1-FLOW-UNBLOCK (HIGH, D-1968; brief l2-rt-side-F-flow-unblock.md sha256 5dbd1f31…, in the card / pasted
    with this brief) in parallel with Q. F5 (stall alarms) builds on /v1/alarms (merged #82 1449b01e) -> no longer waits.
Per lane: MERGE-READY <pr> <head> + CI + seat records INLINE (review V-summary + TEST-GAMING/VERDICT lines + served_model/finish_reason).
BLOCKED <item> | what | options, *default. RECYCLE-READY <card> before 200k.
