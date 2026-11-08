//! Daily runner and JSON close-gate adapter. No keeper credentials required.
use pg_worker_core::live_probe::{close_gate, run, ResultRecord, Spec};
use serde::Deserialize;
use std::fs::OpenOptions;
use std::io::{BufRead, Write};

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Item {
    key: String,
    kind: String,
    live_probe: Option<Spec>,
}

fn now() -> Result<u64, Box<dyn std::error::Error>> {
    Ok(std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH)?.as_secs())
}

fn main() {
    if let Err(e) = execute() {
        eprintln!("live-probe: {e}");
        std::process::exit(1);
    }
}

fn execute() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<_> = std::env::args().skip(1).collect();
    if args.len() != 3 || !["run", "gate"].contains(&args[0].as_str()) {
        return Err("usage: pg-live-probe run|gate ITEMS.json JOURNAL.jsonl".into());
    }
    let items: Vec<Item> = serde_json::from_reader(std::fs::File::open(&args[1])?)?;
    let timestamp = now()?;
    if args[0] == "gate" {
        let records: Vec<ResultRecord> = std::io::BufReader::new(std::fs::File::open(&args[2])?)
            .lines().map(|line| Ok(serde_json::from_str(&line?)?))
            .collect::<Result<_, Box<dyn std::error::Error>>>()?;
        for item in items {
            close_gate(&item.kind, &item.key, item.live_probe.as_ref(), &records, timestamp)?;
        }
    } else {
        let mut journal = OpenOptions::new().create(true).append(true).open(&args[2])?;
        let mut failures = 0;
        for item in items.iter().filter(|i| i.kind == "feature") {
            let result = item.live_probe.as_ref().ok_or("feature missing live_probe")
                .map_err(String::from).and_then(|s| run(&item.key, s, timestamp));
            match result {
                Ok(record) => {
                    serde_json::to_writer(&mut journal, &record)?;
                    writeln!(journal)?;
                    journal.sync_data()?;
                    if !record.passed { failures += 1; eprintln!("probe failed: {}", item.key); }
                }
                Err(e) => { failures += 1; eprintln!("probe error: {}: {e}", item.key); }
            }
        }
        if failures > 0 { return Err(format!("{failures} probe failures").into()); }
    }
    Ok(())
}
