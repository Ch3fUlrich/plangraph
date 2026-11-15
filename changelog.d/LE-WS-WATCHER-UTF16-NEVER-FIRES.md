Added the Rust `pg-run-watch` launcher and watcher probe. The launcher writes an
atomic UTF-8 `EXIT <code>` sidecar beside a unique log; completion no longer depends
on searching PowerShell UTF-16 output through nested SSH/cmd quoting. The watcher
reports finished runs, errors and logs unchanged for 60 minutes. Rust integration
tests exercise a finished and an unfinished real child with UTF-16 output.
See docs/workstation-run-watch.md for polling and target-host acceptance; live
workstation unit replacement remains an operator rollout step.
