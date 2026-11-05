### Added
- Rust workspace bundle export/import primitives with origin merge-base prerequisites,
  shared fetch locking, temporary-ref CAS cleanup, and fail-closed bundle checks.
- Transport only: R5 reachable-content scanning is required before execution or publication.

### Fixed
- Remove a completed export when temporary-ref cleanup fails; test export-only
  source and origin ref invariants, including absence of publication.

### Documentation: export failure contract
- Export uses the R4A Git runner and shared fetch lock. A cleanup error returns
  failure and removes only output created by that export, never an existing file.
- Cleanup uses compare-and-swap deletion; failure does not authorize deleting a
  ref whose value changed. Callers must treat the export as unsuccessful.
