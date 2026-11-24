Collect UTC hourly and daily throughput in the existing landing telemetry
database. Compare commits and keeper closures over time alongside train
duration, native writer activity, acceptance latency, usage and quota samples.
Collection and reporting use bundled SQLite.
The report also gives one acceptance per verified train part head, per-train attempts, drops and
minutes per merged batch, hourly batches merged, and quota rows with account, window and reset.
