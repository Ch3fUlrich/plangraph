//! Priority-ordered, file-disjoint writer batches claimed from simultaneous feeder hosts.
use plangraph_cli::feeder::*;
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::sync::{Arc, Barrier, Mutex};

fn facts(host: &str) -> HostFacts {
    serde_json::from_value(
        json!({"host":host,"free_ram_bytes":8000000000u64,"free_disk_bytes":20000000000u64,
        "quota_remaining_percent":80,"observed_at":1000,"now":1001}),
    )
    .unwrap()
}
fn item(key: &str, priority: i64, files: &[&str]) -> Value {
    json!({"key":format!("plangraph:{key}"),"title":key,"priority":priority,"state":"open",
        "created_at":"2026-01-01T00:00:00Z","note":format!("files: {}", json!(files))})
}
fn ready(items: Vec<Value>) -> Value {
    json!({"items":items})
}
fn policy(batch: usize) -> Policy {
    Policy {
        batch_max_items: batch,
        ..Policy::default()
    }
}

/// The keeper's view that every host shares: claims are atomic per key.
#[derive(Default)]
struct Registry {
    claimed: BTreeMap<String, Vec<String>>,
    started: Vec<Vec<String>>,
    released: Vec<String>,
    /// The epoch the keeper issued for each claim, and each release as (key, epoch).
    epochs: BTreeMap<String, u64>,
    releases: Vec<(String, u64)>,
    /// Each rival check as (key, the batch it was told is this host's own).
    rival_batches: Vec<(String, Vec<String>)>,
}
struct Host {
    registry: Arc<Mutex<Registry>>,
    files: BTreeMap<String, Vec<String>>,
    fail_start: bool,
    fail_release: bool,
    /// The claim of this key fails as unavailable; the rival check of this key errors.
    unavailable_at: Option<String>,
    rival_error_at: Option<String>,
    queued: bool,
    /// The live lane that holds a key, by key.
    lanes: BTreeMap<String, String>,
    /// Held after the first claim until the other host has claimed as well.
    meet: Option<Arc<Barrier>>,
}
impl Host {
    fn new(registry: &Arc<Mutex<Registry>>, items: &[Value]) -> Self {
        let files = items
            .iter()
            .map(|i| {
                let note = i["note"].as_str().unwrap();
                let list: Vec<String> =
                    serde_json::from_str(note.strip_prefix("files: ").unwrap()).unwrap();
                (i["key"].as_str().unwrap().to_owned(), list)
            })
            .collect();
        Self {
            registry: registry.clone(),
            files,
            fail_start: false,
            fail_release: false,
            unavailable_at: None,
            rival_error_at: None,
            queued: false,
            lanes: BTreeMap::new(),
            meet: None,
        }
    }
}
impl Launch for Host {
    fn lane_holder(&mut self, key: &str) -> Result<Option<String>, String> {
        Ok(self.lanes.get(key).cloned())
    }
    fn claim(&mut self, key: &str) -> Result<u64, ClaimError> {
        let mut registry = self.registry.lock().unwrap();
        let result = if self.unavailable_at.as_deref() == Some(key) {
            Err(ClaimError::Unavailable("keeper offline".into()))
        } else if registry.claimed.contains_key(key) {
            Err(ClaimError::Refused("conflict".into()))
        } else {
            registry
                .claimed
                .insert(key.to_owned(), self.files[key].clone());
            // A distinct epoch per claim, so a release with another key's epoch is visible.
            let epoch = 100 + registry.epochs.len() as u64;
            registry.epochs.insert(key.to_owned(), epoch);
            Ok(epoch)
        };
        drop(registry);
        if let Some(barrier) = self.meet.take() {
            barrier.wait();
        }
        result
    }
    fn rival(
        &mut self,
        key: &str,
        files: &[String],
        batch: &[String],
    ) -> Result<Option<String>, String> {
        self.registry
            .lock()
            .unwrap()
            .rival_batches
            .push((key.to_owned(), batch.to_vec()));
        if self.rival_error_at.as_deref() == Some(key) {
            return Err("snapshot unreachable".into());
        }
        let registry = self.registry.lock().unwrap();
        Ok(registry
            .claimed
            .iter()
            .find(|(other, theirs)| {
                *other != key
                    && !batch.contains(other)
                    && theirs.iter().any(|a| files.iter().any(|b| a == b))
            })
            .map(|(other, _)| other.clone()))
    }
    fn start(&mut self, card: &Value, _: &str) -> Result<Value, String> {
        if self.fail_start {
            return Err("spawner denied".into());
        }
        let keys: Vec<String> = card["item_keys"].as_array().map_or_else(
            || vec![card["item_key"].as_str().unwrap().to_owned()],
            |keys| {
                keys.iter()
                    .map(|k| k.as_str().unwrap().to_owned())
                    .collect()
            },
        );
        self.registry.lock().unwrap().started.push(keys);
        Ok(json!({"run_id":"run-1","status":if self.queued {"queued"} else {"running"}}))
    }
    fn release(&mut self, key: &str, epoch: u64) -> Result<(), String> {
        if self.fail_release {
            return Err("release denied".into());
        }
        let mut registry = self.registry.lock().unwrap();
        registry.releases.push((key.to_owned(), epoch));
        // The keeper refuses a release that does not carry the claim's own epoch.
        if registry.epochs.get(key) != Some(&epoch) {
            return Err(format!("epoch_stale: {key} {epoch}"));
        }
        registry.claimed.remove(key);
        registry.released.push(key.to_owned());
        Ok(())
    }
}
fn drive(ready: &Value, host: &mut Host, name: &str, batch: usize, dry: bool) -> Value {
    tick(
        ready,
        &[],
        &facts(name),
        &policy(batch),
        "rules",
        "/repo",
        "/reviews",
        dry,
        host,
    )
}
fn claimed_files(registry: &Registry) -> Vec<(String, Vec<String>)> {
    registry
        .claimed
        .iter()
        .map(|(k, f)| (k.clone(), f.clone()))
        .collect()
}
fn assert_disjoint(registry: &Registry) {
    let claimed = claimed_files(registry);
    for (i, (a, af)) in claimed.iter().enumerate() {
        for (b, bf) in &claimed[i + 1..] {
            assert!(
                !af.iter().any(|x| bf.iter().any(|y| x == y)),
                "{a} and {b} overlap: {claimed:?}"
            );
        }
    }
}

#[test]
fn related_items_share_one_batch_in_priority_order() {
    let items = vec![
        item("ALPHA-2", 5, &["src/a.rs"]),
        item("ALPHA-1", 9, &["src/b.rs"]),
        item("BETA-1", 7, &["src/a.rs"]),
        item("GAMMA-1", 8, &["src/z.rs"]),
    ];
    let registry = Arc::default();
    let mut host = Host::new(&registry, &items);
    let row = drive(&ready(items), &mut host, "dell", 3, false);
    assert_eq!(row["item_key"], "plangraph:ALPHA-1");
    assert_eq!(
        row["item_keys"],
        json!(["plangraph:ALPHA-1", "plangraph:BETA-1", "plangraph:ALPHA-2"])
    );
    // GAMMA-1 has higher priority than BETA-1 but is unrelated to the batch.
    assert!(!registry
        .lock()
        .unwrap()
        .claimed
        .contains_key("plangraph:GAMMA-1"));
}

#[test]
fn one_item_per_run_is_the_default() {
    let items = vec![
        item("ALPHA-1", 9, &["src/a.rs"]),
        item("ALPHA-2", 5, &["src/a.rs"]),
    ];
    let registry = Arc::default();
    let mut host = Host::new(&registry, &items);
    let row = drive(&ready(items), &mut host, "dell", 1, false);
    assert_eq!(row["decision"], "spawned");
    assert!(row.get("item_keys").is_none());
    assert_eq!(registry.lock().unwrap().started, [["plangraph:ALPHA-1"]]);
}

#[test]
fn simultaneous_hosts_never_keep_overlapping_files() {
    let items = vec![
        item("ALPHA-1", 9, &["src/a.rs"]),
        item("BETA-1", 8, &["src/a.rs"]),
    ];
    let registry: Arc<Mutex<Registry>> = Arc::default();
    let meet = Arc::new(Barrier::new(2));
    let doc = ready(items.clone());
    let rows: Vec<Value> = std::thread::scope(|scope| {
        let handles: Vec<_> = [
            ("dell", "plangraph:ALPHA-1"),
            ("coding", "plangraph:BETA-1"),
        ]
        .into_iter()
        .map(|(name, _)| {
            let mut host = Host::new(&registry, &items);
            host.meet = Some(meet.clone());
            let doc = &doc;
            scope.spawn(move || drive(doc, &mut host, name, 1, false))
        })
        .collect();
        handles.into_iter().map(|h| h.join().unwrap()).collect()
    });
    let registry = registry.lock().unwrap();
    // Each host claimed one item before either checked; neither may keep both files.
    assert_disjoint(&registry);
    assert!(registry.started.len() <= 1, "{:?}", registry.started);
    for row in rows {
        assert!(
            matches!(row["decision"].as_str(), Some("spawned" | "idle")),
            "{row}"
        );
    }
}

#[test]
fn a_later_host_yields_to_a_queued_run_and_launches_disjoint_work() {
    let items = vec![
        item("ALPHA-1", 9, &["src/a.rs"]),
        item("BETA-1", 8, &["src/a.rs"]),
        item("GAMMA-1", 1, &["src/z.rs"]),
    ];
    let registry: Arc<Mutex<Registry>> = Arc::default();
    let mut first = Host::new(&registry, &items);
    first.queued = true;
    let one = drive(&ready(items.clone()), &mut first, "dell", 1, false);
    assert_eq!(one["decision"], "pending");
    let mut second = Host::new(&registry, &items);
    let two = drive(&ready(items), &mut second, "coding", 1, false);
    assert_eq!(two["item_key"], "plangraph:GAMMA-1", "{two}");
    let registry = registry.lock().unwrap();
    assert_eq!(
        registry.started,
        [vec!["plangraph:ALPHA-1"], vec!["plangraph:GAMMA-1"]]
    );
    // BETA-1 was claimed, found its file reserved, and released only its own claim.
    assert_eq!(registry.released, ["plangraph:BETA-1"]);
    let beta = two["skipped"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["item_key"] == "plangraph:BETA-1")
        .unwrap();
    assert!(beta["reason"]
        .as_str()
        .unwrap()
        .starts_with("files_reserved_by:plangraph:ALPHA-1"));
    assert_disjoint(&registry);
}

#[test]
fn a_failed_launch_releases_only_its_own_batch() {
    let items = vec![
        item("ALPHA-1", 9, &["src/a.rs"]),
        item("ALPHA-2", 8, &["src/b.rs"]),
        item("GAMMA-1", 1, &["src/z.rs"]),
    ];
    let registry: Arc<Mutex<Registry>> = Arc::default();
    registry
        .lock()
        .unwrap()
        .claimed
        .insert("plangraph:OTHER-HOST".into(), vec!["src/other.rs".into()]);
    let mut host = Host::new(&registry, &items);
    host.fail_start = true;
    let row = drive(&ready(items), &mut host, "dell", 2, false);
    assert_eq!(row["decision"], "refused");
    assert!(row["reason"].as_str().unwrap().contains("claim released"));
    let registry = registry.lock().unwrap();
    assert_eq!(
        registry.released,
        ["plangraph:ALPHA-1", "plangraph:ALPHA-2"]
    );
    assert_eq!(
        registry.claimed.keys().collect::<Vec<_>>(),
        ["plangraph:OTHER-HOST"]
    );
}

/// The batch brief the feeder emits; pg-spawnd renders the same file in `role_header` tests, so
/// a feeder batch gets the batch-writer skill.
const FEEDER_BATCH_INPUT: &str =
    include_str!("../../pg-spawnd/tests/fixtures/feeder-batch-input.txt");

#[test]
fn dry_run_shows_the_batch_without_claiming() {
    let items = vec![
        item("ALPHA-1", 9, &["src/a.rs"]),
        item("ALPHA-2", 8, &["src/b.rs"]),
    ];
    let registry: Arc<Mutex<Registry>> = Arc::default();
    let mut host = Host::new(&registry, &items);
    let row = drive(&ready(items), &mut host, "dell", 2, true);
    assert_eq!(row["decision"], "would_spawn");
    assert_eq!(row["card"]["files"], json!(["src/a.rs", "src/b.rs"]));
    // Compared as JSON: key order is the serializer's, not part of the input.
    let brief = row["brief"].as_str().unwrap();
    let (prefix, payload) = brief.split_once('\n').unwrap();
    let (golden_prefix, golden) = FEEDER_BATCH_INPUT.split_once('\n').unwrap();
    assert_eq!(prefix, golden_prefix);
    let payload: Value = serde_json::from_str(payload).unwrap();
    assert_eq!(payload, serde_json::from_str::<Value>(golden).unwrap());
    assert!(payload["run"]["trailers"]
        .as_array()
        .unwrap()
        .iter()
        .any(|t| t.as_str().unwrap().starts_with("Writer: ")));
    assert!(registry.lock().unwrap().claimed.is_empty());
}

#[test]
fn a_member_claimed_by_another_host_is_a_rival_not_a_batch_mate() {
    // Another host already holds BETA-1; this host plans [ALPHA-1, BETA-1] on one file.
    let items = vec![
        item("ALPHA-1", 9, &["src/a.rs"]),
        item("BETA-1", 8, &["src/a.rs"]),
    ];
    let registry: Arc<Mutex<Registry>> = Arc::default();
    registry
        .lock()
        .unwrap()
        .claimed
        .insert("plangraph:BETA-1".into(), vec!["src/a.rs".into()]);
    let mut host = Host::new(&registry, &items);
    let row = drive(&ready(items), &mut host, "dell", 2, false);
    assert_eq!(row["decision"], "idle", "{row}");
    let registry = registry.lock().unwrap();
    assert!(registry.started.is_empty(), "{:?}", registry.started);
    assert_eq!(registry.released, ["plangraph:ALPHA-1"]);
    assert!(registry.claimed.contains_key("plangraph:BETA-1"));
}

#[test]
fn simultaneous_multi_item_batches_never_start_overlapping_files() {
    let items = vec![
        item("ALPHA-1", 9, &["src/a.rs"]),
        item("BETA-1", 8, &["src/a.rs"]),
    ];
    let doc = ready(items.clone());
    for _ in 0..40 {
        let registry: Arc<Mutex<Registry>> = Arc::default();
        let meet = Arc::new(Barrier::new(2));
        std::thread::scope(|scope| {
            for name in ["dell", "coding"] {
                let mut host = Host::new(&registry, &items);
                host.meet = Some(meet.clone());
                let doc = &doc;
                scope.spawn(move || drive(doc, &mut host, name, 2, false));
            }
        });
        let registry = registry.lock().unwrap();
        // Both items share src/a.rs: at most one run may hold either of them.
        assert!(registry.started.len() <= 1, "{:?}", registry.started);
        if let Some(keys) = registry.started.first() {
            assert!(keys.iter().all(|k| registry.claimed.contains_key(k)));
        }
    }
}

#[test]
fn an_active_batch_run_excludes_open_items_of_every_family_it_holds() {
    let items = vec![
        item("BETA-2", 9, &["src/z.rs"]),
        item("GAMMA-1", 1, &["src/y.rs"]),
    ];
    let run = Run {
        run_id: "run-batch".into(),
        started: Some(900.0),
        item_key: "plangraph:ALPHA-1".into(),
        item_keys: vec!["plangraph:ALPHA-1".into(), "plangraph:BETA-1".into()],
        host: "dell".into(),
        status: "running".into(),
        role: "implement".into(),
        files: vec!["src/a.rs".into()],
    };
    let picked = pick(&ready(items), &[run], &Policy::default()).unwrap();
    assert_eq!(picked.key, "plangraph:GAMMA-1");
}

#[test]
fn a_refused_seed_sends_the_tick_back_to_the_next_priority_not_the_batch_tail() {
    // ALPHA-1 (p9) is held elsewhere; ALPHA-2 (p5) is its family, GAMMA-1 (p8) is unrelated.
    let items = vec![
        item("ALPHA-1", 9, &["src/a.rs"]),
        item("ALPHA-2", 5, &["src/b.rs"]),
        item("GAMMA-1", 8, &["src/z.rs"]),
    ];
    let registry: Arc<Mutex<Registry>> = Arc::default();
    registry
        .lock()
        .unwrap()
        .claimed
        .insert("plangraph:ALPHA-1".into(), vec!["src/a.rs".into()]);
    let mut host = Host::new(&registry, &items);
    let row = drive(&ready(items), &mut host, "dell", 2, false);
    assert_eq!(row["item_key"], "plangraph:GAMMA-1", "{row}");
    let registry = registry.lock().unwrap();
    assert_eq!(registry.started, [vec!["plangraph:GAMMA-1"]]);
    assert!(!registry.claimed.contains_key("plangraph:ALPHA-2"));
}

#[test]
fn a_yielding_seed_releases_its_batch_and_the_tick_picks_again() {
    // Another host holds src/a.rs under a different item: the seed ALPHA-1 yields.
    let items = vec![
        item("ALPHA-1", 9, &["src/a.rs"]),
        item("ALPHA-2", 5, &["src/b.rs"]),
        item("GAMMA-1", 8, &["src/z.rs"]),
    ];
    let registry: Arc<Mutex<Registry>> = Arc::default();
    registry
        .lock()
        .unwrap()
        .claimed
        .insert("plangraph:OTHER".into(), vec!["src/a.rs".into()]);
    let mut host = Host::new(&registry, &items);
    let row = drive(&ready(items), &mut host, "dell", 2, false);
    assert_eq!(row["item_key"], "plangraph:GAMMA-1", "{row}");
    let registry = registry.lock().unwrap();
    assert_eq!(registry.started, [vec!["plangraph:GAMMA-1"]]);
    assert_eq!(
        registry.released[..2],
        ["plangraph:ALPHA-1", "plangraph:ALPHA-2"]
    );
    assert!(!registry.claimed.contains_key("plangraph:ALPHA-2"));
}

#[test]
fn a_release_that_fails_when_the_seed_yields_is_reported() {
    let items = vec![
        item("ALPHA-1", 9, &["src/a.rs"]),
        item("ALPHA-2", 5, &["src/b.rs"]),
    ];
    let registry: Arc<Mutex<Registry>> = Arc::default();
    registry
        .lock()
        .unwrap()
        .claimed
        .insert("plangraph:OTHER".into(), vec!["src/a.rs".into()]);
    let mut host = Host::new(&registry, &items);
    host.fail_release = true;
    let row = drive(&ready(items), &mut host, "dell", 2, false);
    let reasons: Vec<String> = row["skipped"]
        .as_array()
        .unwrap()
        .iter()
        .map(|r| format!("{} {}", r["item_key"], r["reason"]))
        .collect();
    // The yielding seed and the released mate both name their failed release.
    assert!(
        reasons
            .iter()
            .any(|r| r.contains("ALPHA-1") && r.contains("release failed")),
        "{reasons:?}"
    );
    assert!(
        reasons
            .iter()
            .any(|r| r.contains("ALPHA-2") && r.contains("release failed")),
        "{reasons:?}"
    );
}

#[test]
fn a_batch_larger_than_the_record_can_hold_is_an_invalid_policy() {
    let items = vec![item("ALPHA-1", 9, &["src/a.rs"])];
    let registry: Arc<Mutex<Registry>> = Arc::default();
    for (size, refused) in [(MAX_BATCH_ITEMS, false), (MAX_BATCH_ITEMS + 1, true)] {
        let mut host = Host::new(&registry, &items);
        let row = drive(&ready(items.clone()), &mut host, "dell", size, true);
        assert_eq!(row["decision"] == "refused", refused, "{size}: {row}");
        if refused {
            assert_eq!(row["reason"], "invalid_policy");
        }
    }
}

/// Every release carried the epoch of its own claim.
fn assert_released_with_own_epochs(registry: &Registry, expected: &[&str]) {
    let mut keys: Vec<&str> = registry.releases.iter().map(|(k, _)| k.as_str()).collect();
    keys.sort_unstable();
    let mut want = expected.to_vec();
    want.sort_unstable();
    assert_eq!(keys, want, "{:?}", registry.releases);
    for (key, epoch) in &registry.releases {
        assert_eq!(registry.epochs.get(key), Some(epoch), "{key}");
    }
    assert!(
        expected.iter().all(|k| !registry.claimed.contains_key(*k)),
        "{:?}",
        registry.claimed
    );
}

#[test]
fn an_unavailable_claim_mid_batch_releases_the_claimed_members_by_their_own_epochs() {
    let items = vec![
        item("ALPHA-1", 9, &["src/a.rs"]),
        item("ALPHA-2", 8, &["src/b.rs"]),
        item("ALPHA-3", 7, &["src/c.rs"]),
    ];
    let registry: Arc<Mutex<Registry>> = Arc::default();
    let mut host = Host::new(&registry, &items);
    host.unavailable_at = Some("plangraph:ALPHA-3".into());
    let row = drive(&ready(items), &mut host, "dell", 3, false);
    assert_eq!(row["decision"], "refused", "{row}");
    assert_eq!(row["error"], true);
    assert!(row["reason"].as_str().unwrap().contains("keeper offline"));
    let registry = registry.lock().unwrap();
    assert!(registry.started.is_empty());
    assert_ne!(
        registry.epochs["plangraph:ALPHA-1"],
        registry.epochs["plangraph:ALPHA-2"]
    );
    assert_released_with_own_epochs(&registry, &["plangraph:ALPHA-1", "plangraph:ALPHA-2"]);
}

#[test]
fn a_rival_check_error_releases_every_claimed_member_by_its_own_epoch() {
    let items = vec![
        item("ALPHA-1", 9, &["src/a.rs"]),
        item("ALPHA-2", 8, &["src/b.rs"]),
        item("ALPHA-3", 7, &["src/c.rs"]),
    ];
    let registry: Arc<Mutex<Registry>> = Arc::default();
    let mut host = Host::new(&registry, &items);
    host.rival_error_at = Some("plangraph:ALPHA-2".into());
    let row = drive(&ready(items), &mut host, "dell", 3, false);
    assert_eq!(row["decision"], "refused", "{row}");
    assert!(row["reason"]
        .as_str()
        .unwrap()
        .contains("reservation check failed: snapshot unreachable"));
    let registry = registry.lock().unwrap();
    assert!(registry.started.is_empty());
    assert_released_with_own_epochs(
        &registry,
        &[
            "plangraph:ALPHA-1",
            "plangraph:ALPHA-2",
            "plangraph:ALPHA-3",
        ],
    );
}

#[test]
fn a_failed_launch_and_a_yielded_member_release_by_their_own_epochs() {
    // ALPHA-2 (not the seed) yields to another host's claim on its file; the launch then fails.
    let items = vec![
        item("ALPHA-1", 9, &["src/a.rs"]),
        item("ALPHA-2", 8, &["src/b.rs"]),
    ];
    let registry: Arc<Mutex<Registry>> = Arc::default();
    registry
        .lock()
        .unwrap()
        .claimed
        .insert("plangraph:OTHER".into(), vec!["src/b.rs".into()]);
    let mut host = Host::new(&registry, &items);
    host.fail_start = true;
    let row = drive(&ready(items), &mut host, "dell", 2, false);
    assert!(
        row["reason"].as_str().unwrap().contains("claim released"),
        "{row}"
    );
    let registry = registry.lock().unwrap();
    assert_released_with_own_epochs(&registry, &["plangraph:ALPHA-1", "plangraph:ALPHA-2"]);
    assert!(registry.claimed.contains_key("plangraph:OTHER"));
}

#[test]
fn a_rival_check_error_after_a_yield_releases_only_the_claims_still_held() {
    // ALPHA-2 yields to OTHER; the check of ALPHA-3 then errors.
    let items = vec![
        item("ALPHA-1", 9, &["src/a.rs"]),
        item("ALPHA-2", 8, &["src/b.rs"]),
        item("ALPHA-3", 7, &["src/c.rs"]),
    ];
    let registry: Arc<Mutex<Registry>> = Arc::default();
    registry
        .lock()
        .unwrap()
        .claimed
        .insert("plangraph:OTHER".into(), vec!["src/b.rs".into()]);
    let mut host = Host::new(&registry, &items);
    host.rival_error_at = Some("plangraph:ALPHA-3".into());
    let row = drive(&ready(items), &mut host, "dell", 3, false);
    assert_eq!(row["decision"], "refused", "{row}");
    let registry = registry.lock().unwrap();
    // Each claim is released once: the yielded member is not released again.
    assert_released_with_own_epochs(
        &registry,
        &[
            "plangraph:ALPHA-1",
            "plangraph:ALPHA-2",
            "plangraph:ALPHA-3",
        ],
    );
    let told_later: Vec<&String> = registry
        .rival_batches
        .iter()
        .find(|(key, _)| key == "plangraph:ALPHA-3")
        .map(|(_, batch)| batch.iter().collect())
        .unwrap();
    assert!(
        !told_later.iter().any(|k| *k == "plangraph:ALPHA-2"),
        "{told_later:?}"
    );
}

#[test]
fn a_failed_batch_still_reports_the_members_dropped_before_the_failure() {
    // ALPHA-2 is held elsewhere (dropped), then ALPHA-3's claim fails as unavailable.
    let items = vec![
        item("ALPHA-1", 9, &["src/a.rs"]),
        item("ALPHA-2", 8, &["src/b.rs"]),
        item("ALPHA-3", 7, &["src/c.rs"]),
    ];
    let registry: Arc<Mutex<Registry>> = Arc::default();
    registry
        .lock()
        .unwrap()
        .claimed
        .insert("plangraph:ALPHA-2".into(), vec!["src/b.rs".into()]);
    let mut host = Host::new(&registry, &items);
    host.unavailable_at = Some("plangraph:ALPHA-3".into());
    let row = drive(&ready(items), &mut host, "dell", 3, false);
    assert_eq!(row["decision"], "refused", "{row}");
    assert_eq!(row["error"], true);
    let dropped = row["skipped"]
        .as_array()
        .unwrap()
        .iter()
        .find(|r| r["item_key"] == "plangraph:ALPHA-2")
        .expect("the dropped member is reported");
    assert_eq!(dropped["reason"], "conflict");
    assert_released_with_own_epochs(&registry.lock().unwrap(), &["plangraph:ALPHA-1"]);
}

#[test]
fn an_item_another_live_lane_holds_is_dropped_before_any_claim_and_the_next_is_launched() {
    let items = vec![item("HELD", 9, &["a.rs"]), item("FREE", 5, &["b.rs"])];
    let registry = Arc::new(Mutex::new(Registry::default()));
    let mut host = Host::new(&registry, &items);
    host.lanes.insert(
        "plangraph:HELD".into(),
        "lane_collision: plangraph:HELD is held by live lane writer-0673 (run)".into(),
    );
    let row = drive(&ready(items), &mut host, "dell", 1, false);
    let registry = registry.lock().unwrap();
    assert_eq!(registry.started, [["plangraph:FREE"]], "{row}");
    assert!(
        !registry.epochs.contains_key("plangraph:HELD"),
        "never claimed"
    );
    assert_eq!(row["skipped"][0]["item_key"], "plangraph:HELD");
    assert!(row["skipped"][0]["reason"].as_str().unwrap().starts_with(
        "lane_held_by:lane_collision: plangraph:HELD is held by live lane writer-0673"
    ));
}
