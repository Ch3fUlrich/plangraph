# Stuck-work snapshot contract

The watchdog accepts six optional arrays in addition to its existing snapshot.
Omitted arrays are empty, preserving compatibility with existing collectors.
Every entry requires `item`, `owner`, and integer `age_minutes`.
The collector must derive these entries from a consistent read-only snapshot:

| Array | Required true gate | Age measured from | Strict threshold |
| --- | --- | --- | --- |
| `lapsed_leases` | `open_lane` | Lease expiry, with no subsequent reclaim | 30 minutes |
| `ordered_items` | `unclaimed` | Operator order naming the item | 120 minutes |
| `ready_owners` | `no_live_session` | Ready items first present without a live owner session | 60 minutes |
| `idle_capacity` | `capacity_available` | Ready code-only work first coexists with runs below cap and available memory above floor | 30 minutes |
| `go_requests` | `unanswered` | GO-REQ or routing question creation | 60 minutes |
| `merged_reviews` | `in_review` | Merge evidence timestamp while item remains open | 360 minutes |

Rows must cease to qualify after reclaim, claim, session start, exhausted
capacity, answer, or item closure respectively. Gates default to false.
Each qualifying entry produces a separate routing/push event naming condition,
item, owner, and age. Identifiers are JSON-escaped to keep messages on one line.
Existing transition persistence and delivery retries apply. No collector,
service installation, or live keeper mutation is performed by this extension.
