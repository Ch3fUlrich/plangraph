### Placement rules B

- Add typed keeper edit/audit transaction contract with schema validation.
- Add bounded last-known-good keeper-layer cache, only usable on Unavailable.
- Add hermetic transaction, rollback and cache tests. Durable keeper wiring,
  authenticated transport and rollout remain integration dependencies.
- The 400-line cap counts production lines only (202); every file is within
  400 lines, so no size-driven split is needed.
