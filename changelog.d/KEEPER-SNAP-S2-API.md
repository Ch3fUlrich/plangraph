### Added
- Expose read-only S1 filters on `GET /v1/snapshot` (S2a, D-3703), with private uniform validation, blank-value omission, filter echo, and filtered request timing. Unfiltered JSON/text bytes, render, signed snapshots, authentication and writes are unchanged.
