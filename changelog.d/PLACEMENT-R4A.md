### Added
- Rust workspace commit checks and shared common-directory fetch locking.

### Fixed
- Classify non-commit object IDs as unreachable rather than Git execution errors.
- Make the warm-path no-fetch regression independent of existing `FETCH_HEAD`.
