Persist run-token heartbeat, evidence, and submit invocations before sending them. The keeper records each mutation and its receipt in one transaction. Live and replayed writes share the same per-run queue. Old queue entries without a known outcome remain available for routing recovery.

NOTE Postgres Migration 15, `keeper write receipts`, imports legacy outcomes. Restart the keeper after migration and deploy the matching native `plangraph` client for run-token writes.
