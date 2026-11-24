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

fn alpha_pair() -> Vec<Value> {
    vec![
        item("ALPHA-1", 9, &["src/a.rs"]),
        item("ALPHA-2", 8, &["src/b.rs"]),
    ]
}

#[test]
fn a_batch_whose_later_claims_would_outlast_the_deadline_launches_without_them() {
    let items = alpha_pair();
    let registry: Arc<Mutex<Registry>> = Arc::default();
    let mut host = Host::new(&registry, &items);
    // 38 s of preflight, two claims at 19 s each: the second claim, with both releases to
    // follow, would end after the 100 s budget. The seed alone still launches.
    host.clock = 38;
    host.claim_secs = 19;
    let row = drive(&ready(items), &mut host, "dell", 2, false);
    assert_eq!(
        registry.lock().unwrap().started,
        [vec!["plangraph:ALPHA-1"]]
    );
    assert!(registry.lock().unwrap().released.is_empty());
    assert_eq!(row["decision"], "spawned");
    assert!(row["skipped"]
        .as_array()
        .unwrap()
        .iter()
        .any(|s| s["item_key"] == "plangraph:ALPHA-2" && s["reason"] == "tick_budget"));
    // The tick never reached the 110 s service timeout, release included.
    assert!(host.clock + 20 <= 110, "ended at {}", host.clock);
}

#[test]
fn a_late_reservation_check_gives_back_only_the_unchecked_members() {
    let items = alpha_pair();
    let registry: Arc<Mutex<Registry>> = Arc::default();
    let mut host = Host::new(&registry, &items);
    // 38 s of preflight and one-second keeper calls: both members are claimed (38, 39 s) and
    // checked in turn, but the second reservation check would begin at 41 s, when its 60 s
    // reserve no longer fits. The checked seed launches; only the second member is released.
    host.clock = 38;
    host.claim_secs = 1;
    host.rival_secs = 1;
    let row = drive(&ready(items), &mut host, "dell", 2, false);
    assert_eq!(
        registry.lock().unwrap().started,
        [vec!["plangraph:ALPHA-1"]]
    );
    assert_eq!(registry.lock().unwrap().released, ["plangraph:ALPHA-2"]);
    assert_eq!(row["decision"], "spawned");
}

#[test]
fn the_size_limit_keeps_the_member_that_connects_the_batch() {
    // A and B overlap only through the lower-priority C. With room for two members the batch
    // is A and C (connected), never A and B (unrelated).
    let items = vec![
        item("AAA-1", 9, &["src/a.rs"]),
        item("BBB-1", 8, &["src/b.rs"]),
        item("CCC-1", 7, &["src/a.rs", "src/b.rs"]),
    ];
    let registry: Arc<Mutex<Registry>> = Arc::default();
    let mut host = Host::new(&registry, &items);
    host.clock = 38;
    drive(&ready(items), &mut host, "dell", 3, false);
    assert_eq!(
        registry.lock().unwrap().started,
        [vec!["plangraph:AAA-1", "plangraph:CCC-1"]]
    );
}

#[test]
fn the_launch_gets_the_time_left_and_a_batch_is_given_back_when_none_is() {
    let items = vec![item("ALPHA-1", 9, &["src/a.rs"])];
    let registry: Arc<Mutex<Registry>> = Arc::default();
    let mut host = Host::new(&registry, &items);
    host.clock = 38;
    host.claim_secs = 10;
    drive(&ready(items.clone()), &mut host, "dell", 1, false);
    // 100 s budget - 48 s used - 20 s to release the one held claim.
    assert_eq!(host.launch_limit, Some(std::time::Duration::from_secs(32)));
    assert_eq!(registry.lock().unwrap().started.len(), 1);
    // A slow reservation check leaves 12 s: below one call, so no launch and no claim kept.
    let registry: Arc<Mutex<Registry>> = Arc::default();
    let mut host = Host::new(&registry, &items);
    host.clock = 38;
    host.claim_secs = 20;
    host.rival_secs = 10;
    let row = drive(&ready(items), &mut host, "dell", 1, false);
    assert!(registry.lock().unwrap().started.is_empty());
    assert!(registry.lock().unwrap().claimed.is_empty());
    assert_eq!(registry.lock().unwrap().released, ["plangraph:ALPHA-1"]);
    assert!(row["reason"].as_str().unwrap().starts_with("tick_budget"));
}

#[test]
fn a_launch_cut_off_at_its_deadline_keeps_its_claims() {
    let items = vec![item("ALPHA-1", 9, &["src/a.rs"])];
    let registry: Arc<Mutex<Registry>> = Arc::default();
    let mut host = Host::new(&registry, &items);
    host.start_error = Some("spawner exceeded 32s".into());
    host.start_uncertain = true;
    let row = drive(&ready(items), &mut host, "dell", 1, false);
    assert_eq!(row["error"], true);
    assert!(row["reason"].as_str().unwrap().contains("claims retained"));
    // A run may have been admitted: nothing is released.
    assert!(registry.lock().unwrap().released.is_empty());
    assert!(registry
        .lock()
        .unwrap()
        .claimed
        .contains_key("plangraph:ALPHA-1"));
}

#[test]
fn a_batch_of_four_still_launches_with_the_members_that_fit_the_deadline() {
    let items: Vec<Value> = (1..=4)
        .map(|n| item(&format!("ALPHA-{n}"), 10 - n, &[&format!("src/a{n}.rs")]))
        .collect();
    let registry: Arc<Mutex<Registry>> = Arc::default();
    let mut host = Host::new(&registry, &items);
    // 5 s of preflight and instant keeper calls: three claims fit with their releases. The
    // fourth member waits for a later tick (its family is held by the running batch); the
    // tick makes progress instead of claiming four and giving them all back.
    host.clock = 5;
    let row = drive(&ready(items.clone()), &mut host, "dell", 4, false);
    assert_eq!(
        registry.lock().unwrap().started,
        [vec![
            "plangraph:ALPHA-1",
            "plangraph:ALPHA-2",
            "plangraph:ALPHA-3"
        ]]
    );
    assert_ne!(row["decision"], "refused");
    assert!(registry.lock().unwrap().released.is_empty());
    // With 38 s of preflight two claims fit.
    let registry: Arc<Mutex<Registry>> = Arc::default();
    let mut host = Host::new(&registry, &items);
    host.clock = 38;
    drive(&ready(items), &mut host, "dell", 4, false);
    assert_eq!(
        registry.lock().unwrap().started,
        [vec!["plangraph:ALPHA-1", "plangraph:ALPHA-2"]]
    );
}
