Port doctor SSH source checks to `pg-spawnd doctor-sources`, with bounded
parallelism and private transport diagnostics. Python doctor uses subprocess
directly, resolving `rust/target/release/pg-spawnd` relative to the checkout,
then `pg-spawnd` on PATH (no binary-path environment override). Missing helpers
report `unavailable`; failed helper calls report `host query failed`.
