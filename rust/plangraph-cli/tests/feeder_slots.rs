//! A tick fills the available host slots with file-disjoint batches.
mod feeder_support;

use feeder_support::*;
use plangraph_cli::feeder::tick;
use serde_json::Value;
use std::sync::{Arc, Mutex};

fn keys(row: &Value, field: &str) -> Vec<String> {
    row[field]
        .as_array()
        .expect("per-slot decisions")
        .iter()
        .map(|batch| batch["item_key"].as_str().unwrap().to_owned())
        .collect()
}

#[test]
fn dry_run_lists_every_batch_the_live_tick_starts_and_defers_overlapping_work() {
    let items = vec![
        item("AAA-1", 9, &["src/a.rs"]),
        item("OVERLAP-1", 8, &["src/a.rs"]),
        item("BBB-1", 7, &["src/b.rs"]),
        item("CCC-1", 6, &["src/c.rs"]),
    ];
    let mut capacity = facts("coding");
    capacity.free_ram_bytes = 16_000_000_000;
    let expected = ["plangraph:AAA-1", "plangraph:BBB-1", "plangraph:CCC-1"];
    for dry in [true, false] {
        let registry: Arc<Mutex<Registry>> = Arc::default();
        let row = tick(
            &ready(items.clone()),
            &[],
            &capacity,
            &policy(1),
            "rules",
            "/repo",
            "/reviews",
            dry,
            &mut Host::new(&registry, &items),
        );
        assert_eq!(row["decision"], if dry { "would_spawn" } else { "spawned" });
        assert_eq!(
            keys(&row, if dry { "would_start" } else { "started" }),
            expected
        );
        assert_eq!(row["slots"], 3);
        assert_eq!(row["stopped"], "no_eligible_item");
        let registry = registry.lock().unwrap();
        if dry {
            assert!(registry.claimed.is_empty());
            for batch in row["would_start"].as_array().unwrap() {
                assert_eq!(batch["card"]["item_key"], batch["item_key"]);
                assert!(batch["brief"].is_string());
            }
        } else {
            assert_eq!(registry.started, expected.map(|key| vec![key]));
            assert_disjoint(&registry);
        }
    }
}

#[test]
fn a_later_launch_failure_stops_the_tick_but_keeps_its_successful_start() {
    let items = vec![
        item("AAA-1", 9, &["src/a.rs"]),
        item("BBB-1", 8, &["src/b.rs"]),
    ];
    let registry = Arc::default();
    let mut host = Host::new(&registry, &items);
    host.fail_start_after = Some(1);
    let row = drive(&ready(items), &mut host, "dell", 1, false);
    assert_eq!(row["decision"], "spawned");
    assert_ne!(row["error"], true);
    assert_eq!(keys(&row, "started"), ["plangraph:AAA-1"]);
    assert!(row["stopped"].as_str().unwrap().contains("spawner denied"));
    assert_eq!(registry.lock().unwrap().released, ["plangraph:BBB-1"]);
}

#[test]
fn each_dry_run_batch_consumes_one_runs_ram() {
    let items = vec![
        item("AAA-1", 9, &["src/a.rs"]),
        item("BBB-1", 8, &["src/b.rs"]),
        item("CCC-1", 7, &["src/c.rs"]),
    ];
    let registry = Arc::default();
    let row = drive(
        &ready(items.clone()),
        &mut Host::new(&registry, &items),
        "dell",
        1,
        true,
    );
    assert_eq!(
        keys(&row, "would_start"),
        ["plangraph:AAA-1", "plangraph:BBB-1"]
    );
    assert_eq!(row["stopped"], "ram");
    assert!(registry.lock().unwrap().claimed.is_empty());
}
