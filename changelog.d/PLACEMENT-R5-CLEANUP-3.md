### Fixed

- Refuse commit objects larger than 1 MiB before allocating or reading their body during finalize history scanning.
- Verify realistic secrets spanning the 64 KiB scan edge remain detected, retaining the multi-MiB boundedness test.
- Align sibling publish re-execution guards with the SSH test: require exactly one running and passing test.
