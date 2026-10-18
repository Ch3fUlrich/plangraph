# Lane F for build-L2-rt-side: plangraph:RT-P1-FLOW-UNBLOCK (HIGH, prio 1, operator D-1968; claimed by build-L1 epoch 1)
Rules: l2-rules-common.md + l2-rt-side.md budget rules (writers qwen qodercli first, D-1704 order; seats 2 non-Claude families != writer).
Start once D(a) + P(1) are MERGE-READY. Up to 3 writers IN PARALLEL on disjoint files (D-1964). Branch lane/rt-p1-flow-unblock from main;
every commit ends with ONE trailer paragraph `Plan-Item: plangraph:RT-P1-FLOW-UNBLOCK`. Tests: full local suite once before the PR
(pytest -n auto + pytest-postgres), then CI. Each rule gets a test that fails without it.
F1 (keeper/evidence.py + store_verbs.py): a main commit carrying `Plan-Item: <key>` binds the item: commit+ci evidence of a commit
   newer than the item's last release/claim-of-another-principal counts even if it predates the current claim (no "predates the claim"
   refusal when the claimant attaches its own trailer-bound commit); keep refusing commits bound to a different key.
F2 (run-record verifier + reviewer-token path): run records store the item key as an explicit field; RunRecordVerifier and the
   reviewer-token check match that field, not a ~120-char task prefix.
F3 (plan check CLI, e.g. `pg check`): list order-only edges (dep whose own done needs nothing from the dependent's code) and open
   items whose Plan-Item commit is already on main.
F4 (router verbs): router may edit done_criteria/depends_on of a CLAIMED item; each edit logged as an event (who, before, after).
F6 (evidence attach): refuse evidence whose ref the seat host cannot read (e.g. secret-gated Semaphore without a reviewer-readable
   copy); clear error naming the kind.
F5 (/v1/alarms stall alarms: claimed item with no event > 2 h; merged Plan-Item commit with open item) ONLY after
   KEEPER-WORKER-VERBS (predeploy-4, owns /v1/alarms) merged: rebase and add it then.
Done text (verify each): see the item; 2 non-Claude seats + judge (HIGH). MERGE-READY <pr> <head> + CI + seat records inline.
