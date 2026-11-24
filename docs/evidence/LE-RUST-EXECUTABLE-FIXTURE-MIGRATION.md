# LE-RUST-EXECUTABLE-FIXTURE-MIGRATION

Item: `plangraph:LE-RUST-EXECUTABLE-FIXTURE-MIGRATION`. Changed jobs: `Rust workspace on Linux: fmt, clippy,
tests` (it runs `cargo test` over the migrated fixtures). Source: `gh api` of the hosted `tests` workflow,
runs created from 2026-11-24T04:00Z; the fixture migration is on main since `9778eca91`..`e30c03818`.

## Eligibility

A run is eligible when its head contains all six migration commits (`b468042c8`, `9778eca91`, `dc078d4e1`,
`8fe6fe87a`, `e30c03818`, `29a8edb06`) and the Rust job finished `success` or `failure` (not cancelled or
skipped) with `cargo test` output in its log. The log was searched for `ExecutableFileBusy`, `Text file busy`,
`ETXTBSY` and `os error 26`. Reruns count as the one run id (`attempt` is the final attempt).

## Runs (32 eligible)

| run id | attempt | Rust job id | runner id | job result | busy-exec matches |
|---|---|---|---|---|---|
| 45328425222 | 1 | 138223569611 | 1000061742 | success | 0 |
| 45338475207 | 1 | 138256164151 | 1000061970 | failure | 0 |
| 45340810098 | 1 | 138263828271 | 1000061993 | success | 0 |
| 45344565373 | 2 | 138292719909 | 1000062339 | success | 0 |
| 45346770994 | 1 | 138285065105 | 1000062228 | failure | 0 |
| 45346771579 | 1 | 138283391133 | 1000062231 | failure | 0 |
| 45346769973 | 1 | 138285183290 | 1000062239 | failure | 0 |
| 45347547985 | 1 | 138287275395 | 1000062260 | success | 0 |
| 45349030980 | 1 | 138290669514 | 1000062283 | success | 0 |
| 45349095534 | 1 | 138290879309 | 1000062293 | success | 0 |
| 45349198852 | 1 | 138291187805 | 1000062308 | failure | 0 |
| 45349233698 | 1 | 138291350532 | 1000062325 | failure | 0 |
| 45350176372 | 1 | 138294476043 | 1000062353 | failure | 0 |
| 45350543472 | 2 | 138309398878 | 1000062371 | success | 0 |
| 45351132489 | 1 | 138297644808 | 1000062384 | success | 0 |
| 45352377896 | 1 | 138301863358 | 1000062401 | success | 0 |
| 45355645706 | 1 | 138312763648 | 1000062423 | success | 0 |
| 45355781047 | 1 | 138313201652 | 1000062434 | success | 0 |
| 45355931023 | 1 | 138313746951 | 1000062452 | success | 0 |
| 45356093255 | 1 | 138314290731 | 1000062461 | success | 0 |
| 45356287145 | 1 | 138314923003 | 1000062474 | success | 0 |
| 45356382483 | 1 | 138315258020 | 1000062492 | success | 0 |
| 45356473047 | 1 | 138316829859 | 1000062508 | success | 0 |
| 45356606093 | 1 | 138316835778 | 1000062514 | failure | 0 |
| 45365578178 | 1 | 138346387977 | 1000062720 | success | 0 |
| 45365882966 | 1 | 138347516168 | 1000062736 | success | 0 |
| 45370719249 | 1 | 138364129785 | 1000062753 | success | 0 |
| 45376229123 | 1 | 138383291033 | 1000062788 | success | 0 |
| 45376508081 | 1 | 138384207029 | 1000062801 | success | 0 |
| 45376763871 | 1 | 138385140998 | 1000062817 | success | 0 |
| 45382096214 | 1 | 138403557894 | 1000062839 | success | 0 |
| 45382435951 | 1 | 138404812052 | 1000062855 | success | 0 |

Runs without an ExecutableFileBusy fixture error: 32 of 32 (target 20). A `failure` row failed
on another step; no failed test name was printed for any row.

## Audit of other publication forms (code, `rust/worker-core/tests/executable_fixtures.rs`)

- Computed modes (`from_mode(mode)`, `.mode(spec.mode)`): 5 sites, all exempt with a reason: the shared writer,
  one production remote-step writer, and three tests that walk a credential file or a directory through modes.
- `fs::copy` into a fixture directory: 12 sites; 3 executed copies (the plangraph binary in `cli.rs`, the
  `commit-msg` hook binary, the install-test hook scripts) now use `write_executable`; the rest are production
  installers or data (exempt by file, with reasons).
