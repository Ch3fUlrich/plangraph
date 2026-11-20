//! Fixture/snapshot interface shared by digest, heartbeat and scheduler callers.
use pg_worker_core::host_idle::Snapshot;
use std::{env, fs};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<_> = env::args_os().collect();
    if args.len() != 2 {
        return Err("usage: host-idle-capacity SNAPSHOT.json".into());
    }
    let snapshot: Snapshot = serde_json::from_slice(&fs::read(&args[1])?)?;
    for message in snapshot.messages()? {
        println!("{message}");
    }
    Ok(())
}
