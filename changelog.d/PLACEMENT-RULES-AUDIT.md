### Placement rules B (checkpoint; re-split required)

- Add typed keeper edit/audit transaction contract with schema validation.
- Add bounded last-known-good keeper-layer cache, only usable on Unavailable.
- Add hermetic transaction, rollback and cache tests. Durable keeper wiring,
  authenticated transport and rollout remain integration dependencies.
- Not merge-ready: production plus tests exceed the requested total-line budget.
