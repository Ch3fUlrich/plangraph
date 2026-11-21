A part enters a train only after its seconds-fast checks and its entry set passed on its head
(`ci_select.py entry-set`, native Windows for any change that selects Rust CI, latest-attempt and flake
disposition rule); part pull requests (label `land-part`) run affected-only CI and `land/train-*` heads the
full suite; `pg-policy --part-entry` refuses imported_history, undeclared_dependency and shared_history;
`.gitattributes` is a gate path; the cost per part is written to telemetry.
Admission requires the part's own Tests run and a successful ci-gate, with reruns ordered by run ID
and attempt number. An unfetchable outside PR is compared using its advertised commit if available locally.
Part cost telemetry matches its Tests run by both pull request number and head.
