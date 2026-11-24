# LE-RUST-EXECUTABLE-FIXTURE-MIGRATION

Item: `plangraph:LE-RUST-EXECUTABLE-FIXTURE-MIGRATION`. Job: `Rust workspace on Linux: fmt, clippy, tests`
(runs `cargo test` over the migrated fixtures). Source: `gh api` of the hosted `tests` workflow, runs created from
2026-11-24T04:00Z, refs `main` pushes and `land/train-*` pull requests only (no lane runs).

## Eligibility

The head contains all six migration commits (`b468042c8`, `9778eca91`, `dc078d4e1`, `8fe6fe87a`, `e30c03818`,
`29a8edb06`) and the Rust job finished `success` or `failure` with `cargo test` output. The log was searched for
`ExecutableFileBusy`, `Text file busy`, `ETXTBSY`, `os error 26`. Reruns count once (`attempt` is the final one).

## Runs (22 eligible)

| run id | attempt | ref | Rust job id | runner id | job result | busy-exec matches |
|---|---|---|---|---|---|---|
| 45338475207 | 1 | train 32 | 138256164151 | 1000061970 | failure | 0 |
| 45340810098 | 1 | train 33 | 138263828271 | 1000061993 | success | 0 |
| 45344565373 | 2 | main | 138292719909 | 1000062339 | success | 0 |
| 45365578178 | 1 | train 34 | 138346387977 | 1000062720 | success | 0 |
| 45365882966 | 1 | train 35 | 138347516168 | 1000062736 | success | 0 |
| 45370719249 | 1 | train 101 | 138364129785 | 1000062753 | success | 0 |
| 45376229123 | 1 | main | 138383291033 | 1000062788 | success | 0 |
| 45376508081 | 1 | train 37 | 138384207029 | 1000062801 | success | 0 |
| 45376763871 | 1 | train 38 | 138385140998 | 1000062817 | success | 0 |
| 45382096214 | 1 | main | 138403557894 | 1000062839 | success | 0 |
| 45382435951 | 1 | train 39 | 138404812052 | 1000062855 | success | 0 |
| 45385108859 | 1 | train 40 | 138414025163 | 1000062873 | success | 0 |
| 45386891895 | 1 | main | 138420125462 | 1000062888 | success | 0 |
| 45389876685 | 1 | train 41 | 138430574371 | 1000062899 | failure | 0 |
| 45392098779 | 1 | train 102 | 138438342445 | 1000062921 | success | 0 |
| 45392469247 | 1 | train 42 | 138439622435 | 1000062932 | success | 0 |
| 45392622072 | 1 | train 43 | 138440132794 | 1000062955 | success | 0 |
| 45395250720 | 1 | train 44 | 138449334253 | 1000062974 | success | 0 |
| 45395621361 | 1 | main | 138450572911 | 1000062992 | success | 0 |
| 45395984814 | 1 | train 45 | 138451912038 | 1000063007 | success | 0 |
| 45397812809 | 1 | train 46 | 138458305890 | 1000063022 | success | 0 |
| 45415062523 | 1 | train 47 | 138519019453 | 1000063052 | success | 0 |

Runs without an ExecutableFileBusy fixture error: 22 of 22. Target of 20 met.
A `failure` row failed on another step; no failed test name was printed for any row.

Audit of other publication forms: see `rust/worker-core/tests/executable_fixtures.rs` (computed modes, literals
inside expressions, `fs::copy` into fixture directories; each exemption names its file and reason).
