//! Shared fake keeper registry and host for the feeder batch and slot tests.
#![allow(dead_code)]
use plangraph_cli::feeder::*;
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::sync::{Arc, Barrier, Mutex};

pub fn facts(host: &str) -> HostFacts {
    serde_json::from_value(
        json!({"host":host,"free_ram_bytes":8000000000u64,"free_disk_bytes":20000000000u64,
        "quota_remaining_percent":80,"observed_at":1000,"now":1001}),
    )
    .unwrap()
}
pub fn item(key: &str, priority: i64, files: &[&str]) -> Value {
    json!({"key":format!("plangraph:{key}"),"title":key,"priority":priority,"state":"open",
        "created_at":"2026-01-01T00:00:00Z","note":format!("files: {}", json!(files))})
}
pub fn ready(items: Vec<Value>) -> Value {
    json!({"items":items})
}
pub fn policy(batch: usize) -> Policy {
    Policy {
        batch_max_items: batch,
        ..Policy::default()
    }
}

/// The keeper's view that every host shares: claims are atomic per key.
#[derive(Default)]
pub struct Registry {
    pub claimed: BTreeMap<String, Vec<String>>,
    pub started: Vec<Vec<String>>,
    pub released: Vec<String>,
    /// The epoch the keeper issued for each claim, and each release as (key, epoch).
    pub epochs: BTreeMap<String, u64>,
    pub releases: Vec<(String, u64)>,
    /// Each rival check as (key, the batch it was told is this host's own).
    pub rival_batches: Vec<(String, Vec<String>)>,
}
pub struct Host {
    pub registry: Arc<Mutex<Registry>>,
    pub files: BTreeMap<String, Vec<String>>,
    pub fail_start: bool,
    /// Starts fail once this many batches have started.
    pub fail_start_after: Option<usize>,
    pub fail_release: bool,
    /// The claim of this key fails as unavailable; the rival check of this key errors.
    pub unavailable_at: Option<String>,
    pub rival_error_at: Option<String>,
    pub queued: bool,
    /// Held after the first claim until the other host has claimed as well.
    pub meet: Option<Arc<Barrier>>,
}
impl Host {
    pub fn new(registry: &Arc<Mutex<Registry>>, items: &[Value]) -> Self {
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
            fail_start_after: None,
            fail_release: false,
            unavailable_at: None,
            rival_error_at: None,
            queued: false,
            meet: None,
        }
    }
}
impl Launch for Host {
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
        let started = self.registry.lock().unwrap().started.len();
        if self.fail_start || self.fail_start_after.is_some_and(|n| started >= n) {
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
pub fn drive(ready: &Value, host: &mut Host, name: &str, batch: usize, dry: bool) -> Value {
    drive_with(ready, host, facts(name), batch, dry)
}
pub fn drive_with(
    ready: &Value,
    host: &mut Host,
    facts: HostFacts,
    batch: usize,
    dry: bool,
) -> Value {
    tick(
        ready,
        &[],
        &facts,
        &policy(batch),
        "rules",
        "/repo",
        "/reviews",
        dry,
        host,
    )
}
pub fn claimed_files(registry: &Registry) -> Vec<(String, Vec<String>)> {
    registry
        .claimed
        .iter()
        .map(|(k, f)| (k.clone(), f.clone()))
        .collect()
}
pub fn assert_disjoint(registry: &Registry) {
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
