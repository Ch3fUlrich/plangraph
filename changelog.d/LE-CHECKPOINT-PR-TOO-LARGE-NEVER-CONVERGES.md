Add the Rust `checkpoint-size BASE HEAD` packaging gate: report committed PR
diff bytes and changed paths, refusing at 100,000 bytes or above or over 25
files. Require the check and stacked splits before phase-batch pushes/seats.
Record the eight named PRs' size/convergence evidence across November 13–14,
with missing historical native review rounds explicitly left open for
PHASE-BATCH-SIZE-EXPERIMENT.
