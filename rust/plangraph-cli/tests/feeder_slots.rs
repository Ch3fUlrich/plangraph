//! A tick fills every slot its host has: the dry run lists what the live tick starts.
mod feeder_support;
use feeder_support::*;
use plangraph_cli::feeder::*;
use serde_json::Value;
use std::sync::{Arc, Mutex};

fn would_keys(row: &Value) -> Vec<String> {
    row["would_start"]
        .as_array()
        .unwrap()
        .iter()
        .map(|w| w["item_key"].as_str().unwrap().to_owned())
        .collect()
}

#[test]
fn dry_run_lists_every_batch_the_tick_would_start_with_disjoint_files() {
    let items = vec![
        item("AAA-1", 9, &["src/a.rs"]),
        item("BBB-1", 8, &["src/a.rs"]),
        item("CCC-1", 7, &["src/c.rs"]),
    ];
    let registry: Arc<Mutex<Registry>> = Arc::default();
    let mut host = Host::new(&registry, &items);
    let row = drive(&ready(items), &mut host, "dell", 1, true);
    // The first decision keeps its fields; the list holds both file-disjoint batches.
    assert_eq!(row["decision"], "would_spawn");
    assert_eq!(row["item_key"], "plangraph:AAA-1");
    assert_eq!(would_keys(&row), ["plangraph:AAA-1", "plangraph:CCC-1"]);
    assert_eq!(row["slots"], 2);
    assert_eq!(row["stopped"], "no_eligible_item");
    for w in row["would_start"].as_array().unwrap() {
        assert!(w["card"]["item_key"].is_string() && w["brief"].is_string());
    }
    assert!(registry.lock().unwrap().claimed.is_empty());
}

#[test]
fn dry_run_list_stops_where_admission_refuses_and_names_why() {
    // 8 GB free with 4 GB per writer: two slots, the third item is refused for RAM.
    let items = vec![
        item("AAA-1", 9, &["src/a.rs"]),
        item("BBB-1", 8, &["src/b.rs"]),
        item("CCC-1", 7, &["src/c.rs"]),
    ];
    let registry: Arc<Mutex<Registry>> = Arc::default();
    let mut host = Host::new(&registry, &items);
    let row = drive(&ready(items), &mut host, "dell", 1, true);
    assert_eq!(would_keys(&row), ["plangraph:AAA-1", "plangraph:BBB-1"]);
    assert_eq!(row["stopped"], "ram");
    // The very first refusal is still a plain refusal, with no list.
    let items = vec![item("AAA-1", 9, &["src/a.rs"])];
    let mut host = Host::new(&registry, &items);
    let mut poor = facts("dell");
    poor.free_ram_bytes = 1;
    let refused = tick(
        &ready(items),
        &[],
        &poor,
        &policy(1),
        "rules",
        "/repo",
        "/reviews",
        true,
        &mut host,
    );
    assert_eq!(refused["decision"], "refused");
    assert_eq!(refused["reason"], "ram");
}

#[test]
fn a_live_tick_starts_the_same_batches_a_dry_run_lists() {
    let items = vec![
        item("AAA-1", 9, &["src/a.rs"]),
        item("BBB-1", 8, &["src/a.rs"]),
        item("CCC-1", 7, &["src/c.rs"]),
    ];
    let dry_registry: Arc<Mutex<Registry>> = Arc::default();
    let dry = drive(
        &ready(items.clone()),
        &mut Host::new(&dry_registry, &items),
        "dell",
        1,
        true,
    );
    let registry: Arc<Mutex<Registry>> = Arc::default();
    let mut host = Host::new(&registry, &items);
    let live = drive(&ready(items), &mut host, "dell", 1, false);
    let started: Vec<_> = live["started"]
        .as_array()
        .unwrap()
        .iter()
        .map(|r| r["item_key"].as_str().unwrap().to_owned())
        .collect();
    assert_eq!(started, would_keys(&dry));
    assert_eq!(live["slots"], dry["slots"]);
    assert_eq!(live["stopped"], dry["stopped"]);
    assert_eq!(live["decision"], "spawned");
    assert_eq!(
        registry.lock().unwrap().started,
        [vec!["plangraph:AAA-1"], vec!["plangraph:CCC-1"]]
    );
    assert_disjoint(&registry.lock().unwrap());
}

#[test]
fn a_launch_failure_after_a_started_batch_still_lists_the_started_one() {
    let items = vec![
        item("AAA-1", 9, &["src/a.rs"]),
        item("BBB-1", 8, &["src/b.rs"]),
    ];
    let registry: Arc<Mutex<Registry>> = Arc::default();
    let mut host = Host::new(&registry, &items);
    host.fail_start_after = Some(1);
    let row = drive(&ready(items), &mut host, "dell", 1, false);
    assert_eq!(row["decision"], "refused");
    assert_eq!(row["error"], true);
    assert_eq!(row["started"].as_array().unwrap().len(), 1);
    assert_eq!(registry.lock().unwrap().released, ["plangraph:BBB-1"]);
}

fn items3() -> Vec<Value> {
    vec![
        item("AAA-1", 9, &["src/a.rs"]),
        item("BBB-1", 8, &["src/b.rs"]),
        item("CCC-1", 7, &["src/c.rs"]),
    ]
}

#[test]
fn a_slow_later_batch_is_not_claimed_when_it_cannot_finish_before_the_service_timeout() {
    let items = items3();
    let registry: Arc<Mutex<Registry>> = Arc::default();
    let mut host = Host::new(&registry, &items);
    // 38 s of preflight, then 20 s to claim and 20 s to launch each batch (100 s budget).
    host.clock = 38;
    host.claim_secs = 20;
    host.start_secs = 20;
    let row = drive(&ready(items), &mut host, "dell", 1, false);
    // The first attempt fits (38 + 60 <= 100); a second would begin at 78 s and run past 110 s.
    assert_eq!(registry.lock().unwrap().started, [vec!["plangraph:AAA-1"]]);
    assert!(row["stopped"].as_str().unwrap().starts_with("tick_budget"));
    assert_eq!(registry.lock().unwrap().claimed.len(), 1);
    assert_eq!(row["decision"], "spawned");
}

#[test]
fn refused_attempts_count_against_the_budget_before_any_writer_starts() {
    let items = items3();
    let registry: Arc<Mutex<Registry>> = Arc::default();
    // The top item is claimed elsewhere: that attempt only burns 20 s of claim time.
    registry
        .lock()
        .unwrap()
        .claimed
        .insert("plangraph:AAA-1".into(), vec!["src/a.rs".into()]);
    let mut host = Host::new(&registry, &items);
    host.clock = 30;
    host.claim_secs = 20;
    host.start_secs = 20;
    let row = drive(&ready(items.clone()), &mut host, "dell", 1, false);
    // At 50 s the next attempt would need 60 s more: no further claim, nothing started.
    assert!(registry.lock().unwrap().started.is_empty());
    assert_eq!(row["decision"], "refused");
    assert!(row["reason"].as_str().unwrap().starts_with("tick_budget"));
    // A preflight that already used 50 s leaves no room for even the first attempt.
    let registry: Arc<Mutex<Registry>> = Arc::default();
    let mut host = Host::new(&registry, &items);
    host.clock = 50;
    let row = drive(&ready(items.clone()), &mut host, "dell", 1, false);
    assert_eq!(row["decision"], "refused");
    assert!(registry.lock().unwrap().claimed.is_empty());
    // A dry run is never limited by the deadline.
    let row = drive(&ready(items), &mut host, "dell", 1, true);
    assert_eq!(row["decision"], "would_spawn");
}
