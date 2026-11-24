//! Pool-use check shared by digest, heartbeat and scheduler callers: reads one JSON
//! sample (a pool object or an array of them), prints the digest line, the alert and
//! the `START <leg> <runs>` plan, and appends a telemetry row when a file is named.
use pg_worker_core::pool_use::{record, Pool};
use std::{env, fs, path::Path};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<_> = env::args_os().collect();
    if args.len() < 2 || args.len() > 3 {
        return Err("usage: pool-use-check SAMPLE.json [TELEMETRY.jsonl]".into());
    }
    let bytes = fs::read(&args[1])?;
    let pools: Vec<Pool> = serde_json::from_slice::<Vec<Pool>>(&bytes)
        .or_else(|_| serde_json::from_slice::<Pool>(&bytes).map(|p| vec![p]))?;
    for pool in pools {
        let report = pool.report()?;
        println!("{}", report.digest_line());
        if let Some(alert) = report.alert() {
            println!("{alert}");
        }
        for (leg, runs) in &report.starts {
            println!("START {leg} {runs}");
        }
        if let Some(path) = args.get(2) {
            record(Path::new(path), &report)?;
        }
    }
    Ok(())
}
