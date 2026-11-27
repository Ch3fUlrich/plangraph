//! Timer-driven activity boundaries. Selection/admission/briefs are pure;
//! spawn is an effectful activity behind `Launch` (including compensating release).
mod adapters;
mod capacity;
mod lanes;
pub(crate) mod pool;
pub(crate) mod quota;
mod runtime;
mod selection;
mod sources;
mod windows_ram;
mod workflow;
pub(crate) use adapters::MERGE_INDEX_EXPIRED;
pub use adapters::{Env, SlowKeeper, Transport};
pub use quota::run_status as quota_status;
pub use runtime::run;
pub use selection::{decode_task, pick, pick_batch, prepare_ready, validate_ready};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::BTreeMap;
pub use workflow::{tick, ClaimError, SpawnError};

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Task {
    pub key: String,
    #[serde(default, deserialize_with = "null_default")]
    pub title: String,
    #[serde(default)]
    pub description: Option<String>,
    #[serde(default)]
    pub done_criteria: Value,
    #[serde(default, deserialize_with = "null_default")]
    pub files: Vec<String>,
    #[serde(default)]
    pub priority: i64,
    #[serde(default)]
    pub created_at: Option<String>,
    #[serde(default)]
    pub state: Option<String>,
    #[serde(flatten)]
    pub fields: BTreeMap<String, Value>,
}

/// Most items one batch run holds: the run record keeps at most this many `item_keys`.
pub const MAX_BATCH_ITEMS: usize = 50;
#[derive(Clone, Debug, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Policy {
    pub max_writers: BTreeMap<String, usize>,
    pub host_aliases: BTreeMap<String, String>,
    pub quota_max_age_seconds: i64,
    pub quota_facts_max_age_seconds: i64,
    pub ram_per_run_bytes: u64,
    pub ram_floor_bytes: u64,
    pub windows_ram_floor_bytes: u64,
    pub disk_floor_bytes: u64,
    pub quota_floor_percent: f64,
    pub cpu_pressure_max_percent: f64,
    pub facts_max_age_seconds: i64,
    pub estimate_tokens: u64,
    /// Related ready items one writer batch may hold; 1 keeps one item per run.
    pub batch_max_items: usize,
    /// The feeder stops a host when this many of its last `no_commit_window` ended feeder runs
    /// ended without a commit; 0 turns the stop off.
    pub no_commit_stop: usize,
    pub no_commit_window: usize,
}
impl Default for Policy {
    fn default() -> Self {
        Self {
            max_writers: BTreeMap::from([("coding".into(), 3), ("dell".into(), 5)]),
            host_aliases: BTreeMap::new(),
            quota_max_age_seconds: 10_800,
            quota_facts_max_age_seconds: 900,
            ram_per_run_bytes: 4_000_000_000,
            ram_floor_bytes: 3_000_000_000,
            windows_ram_floor_bytes: 6_000_000_000,
            disk_floor_bytes: 10_000_000_000,
            quota_floor_percent: 10.0,
            cpu_pressure_max_percent: 50.0,
            facts_max_age_seconds: 300,
            estimate_tokens: 100_000,
            batch_max_items: 6,
            no_commit_stop: 3,
            no_commit_window: 5,
        }
    }
}

#[derive(Clone, Debug, Default, Deserialize)]
pub struct Run {
    #[serde(default, deserialize_with = "null_default")]
    pub run_id: String,
    #[serde(default)]
    pub started: Option<f64>,
    #[serde(default, deserialize_with = "null_default")]
    pub item_key: String,
    #[serde(default, deserialize_with = "null_default")]
    pub host: String,
    #[serde(default, deserialize_with = "null_default")]
    pub status: String,
    #[serde(default, deserialize_with = "null_default")]
    pub role: String,
    #[serde(default, deserialize_with = "null_default")]
    pub files: Vec<String>,
    /// Every item of a batch run; empty for a run of one item.
    #[serde(default, deserialize_with = "null_default")]
    pub item_keys: Vec<String>,
    /// Why the run ended (`no_commit: ...` when its last writer turn added no commit).
    #[serde(default, deserialize_with = "null_default")]
    pub reason: String,
    /// The run was started by the feeder: its card pushes to a `lane/feeder-` branch.
    #[serde(skip)]
    pub feeder: bool,
}
impl Run {
    /// Whether this run works on `key`, as its item or as a member of its batch.
    pub fn holds(&self, key: &str) -> bool {
        self.item_key == key || self.item_keys.iter().any(|k| k == key)
    }
    pub fn active(&self) -> bool {
        !pg_worker_core::run::record::releases_ownership(&self.status)
    }
    pub fn diagnostic(&self) -> bool {
        self.run_id.is_empty()
            && self.item_key.is_empty()
            && self.role.is_empty()
            && self.status == "unknown"
    }
    pub fn writer(&self) -> bool {
        !self.diagnostic() && self.active() && self.role != "review" && self.role != "research"
    }
}

fn null_default<'de, D, T>(de: D) -> Result<T, D::Error>
where
    D: serde::Deserializer<'de>,
    T: Deserialize<'de> + Default,
{
    Ok(Option::<T>::deserialize(de)?.unwrap_or_default())
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct HostFacts {
    pub host: String,
    pub free_ram_bytes: u64,
    pub free_disk_bytes: u64,
    pub quota_remaining_percent: Option<f64>,
    #[serde(default)]
    pub cpu_pressure_avg300: Option<f64>,
    /// Unix seconds; admission compares with the caller's clock, not an internal clock.
    pub observed_at: i64,
    #[serde(default)]
    pub running_writers: usize,
    #[serde(default)]
    pub kill_switch: bool,
    #[serde(default)]
    pub now: i64,
}
#[derive(Debug, PartialEq, Eq, Serialize)]
#[serde(tag = "admission", content = "reason", rename_all = "snake_case")]
pub enum Admission {
    Admit,
    Refuse(String),
}

pub fn admit(f: &HostFacts, p: &Policy) -> Admission {
    let limit = writer_limit(&f.host, p);
    let reason = if f.kill_switch {
        Some("kill_switch".to_string())
    } else if !p.quota_floor_percent.is_finite()
        || !(0.0..100.0).contains(&p.quota_floor_percent)
        || !(0.0..=100.0).contains(&p.cpu_pressure_max_percent)
        || p.facts_max_age_seconds < 0
        || p.quota_max_age_seconds < 0
        || p.quota_facts_max_age_seconds < 0
        || p.ram_per_run_bytes == 0
        || p.disk_floor_bytes == 0
        || p.estimate_tokens == 0
        || !(1..=MAX_BATCH_ITEMS).contains(&p.batch_max_items)
    {
        Some("invalid_policy".to_string())
    } else if f.observed_at > f.now || f.now.saturating_sub(f.observed_at) > p.facts_max_age_seconds
    {
        Some("stale_facts".to_string())
    } else if limit.is_none() {
        Some(format!("unknown_host:{}", f.host))
    } else if let Some(pressure) = f
        .cpu_pressure_avg300
        .filter(|pressure| *pressure > p.cpu_pressure_max_percent)
    {
        Some(format!(
            "cpu_pressure avg300={pressure} > {}",
            p.cpu_pressure_max_percent
        ))
    } else if f.running_writers >= limit.unwrap_or(0) {
        Some("max_writers".to_string())
    } else if f.free_ram_bytes < p.ram_per_run_bytes {
        Some("ram".to_string())
    } else if f.free_disk_bytes < p.disk_floor_bytes {
        Some("disk".to_string())
    } else if !f
        .quota_remaining_percent
        .is_some_and(|q| q.is_finite() && q > p.quota_floor_percent && q <= 100.0)
    {
        Some("provider_quota".to_string())
    } else {
        None
    };
    reason.map_or(Admission::Admit, Admission::Refuse)
}

/// The writer cap the policy lists for `host`: its full name first (private policies name
/// full hosts; public defaults use short aliases), then its canonical identity, then the
/// first DNS label. `None` for a host the policy does not list.
pub fn writer_limit(host: &str, p: &Policy) -> Option<usize> {
    let canonical = canonical_host(host, p);
    p.max_writers
        .get(host)
        .or_else(|| p.max_writers.get(&canonical))
        .or_else(|| {
            p.max_writers
                .get(canonical.split('.').next().unwrap_or(&canonical))
        })
        .copied()
}

pub fn brief(item: &Task, house_rules: &str) -> String {
    let done = item
        .done_criteria
        .as_str()
        .map(str::to_string)
        .unwrap_or_else(|| item.done_criteria.to_string());
    let metadata: BTreeMap<_, _> = [
        "pr",
        "pr_url",
        "pr_number",
        "pull_request",
        "open_pr",
        "ci_fix",
    ]
    .into_iter()
    .filter_map(|k| item.fields.get(k).map(|v| (k, v)))
    .collect();
    format!("Plan item: {}\nTitle: {}\n\n{}\n\nDone criteria:\n{}\n\nFiles:\n{}\n\nNotes:\n{}\n\nTask metadata:\n{}\n\n{}\n",
        item.key,item.title,item.description.as_deref().unwrap_or(""),done,files_section(&item.files),
        item.fields.get("note").and_then(Value::as_str).unwrap_or(""),serde_json::to_string(&metadata).expect("JSON values"),
        house_rules.replace("{{item_key}}",&item.key).trim())
}

/// The brief's file list. A keeper item carries no write set unless a spec gave it one, so an
/// empty list hands the scope to the writer instead of leaving a section the writer reads as
/// "no file may change".
pub fn files_section(files: &[String]) -> String {
    if files.is_empty() {
        NO_FILES_LISTED.into()
    } else {
        files.join("\n")
    }
}
pub const NO_FILES_LISTED: &str = "None listed. Choose the files this item needs inside this \
repository from its description and done criteria, keep the change inside the item, and name \
every changed file in your final message. The run worktree is a detached checkout of main: \
commit on it; the native host pushes the accepted head to the run's lane branch.";

/// Why the feeder starts no writer on `host` now, from the host's own feeder runs: the stop when
/// `no_commit_stop` of the last `no_commit_window` ended runs added no commit, and the canary
/// while no ended run of that window committed and one run is still going.
pub fn feeder_stop(runs: &[Run], host: &str, p: &Policy) -> Option<String> {
    let mut mine: Vec<&Run> = runs
        .iter()
        .filter(|r| r.feeder && r.role == "implement" && same_host(&r.host, host, p))
        .collect();
    mine.sort_by(|a, b| {
        b.started
            .unwrap_or(0.0)
            .total_cmp(&a.started.unwrap_or(0.0))
    });
    let ended: Vec<&Run> = mine
        .iter()
        .copied()
        .filter(|r| !r.active())
        .take(p.no_commit_window)
        .collect();
    let without = ended.iter().filter(|r| barren(r)).count();
    if p.no_commit_stop > 0 && without >= p.no_commit_stop {
        return Some(format!(
            "no_commit_stop: {without} of the last {} ended feeder runs on this host added no commit",
            ended.len()
        ));
    }
    let proven = ended.iter().any(|r| !barren(r));
    if !proven && mine.iter().any(|r| r.active()) {
        return Some(
            "canary_pending: the host's first feeder run has not ended with a commit".into(),
        );
    }
    None
}

fn barren(r: &Run) -> bool {
    r.reason.starts_with("no_commit") || r.reason == "writer_no_changes"
}

pub fn card(item: &Task, repo: &str, review_dir: &str, policy: &Policy) -> Value {
    json!({"run_kind":"native-codex", "repo":repo, "base_ref":"refs/heads/main",
        "item_key":item.key, "review_dir":review_dir, "estimate_tokens":policy.estimate_tokens, "files":item.files})
}

/// Registry aliases and short DNS names share one identity for policy and counting.
pub fn canonical_host(host: &str, p: &Policy) -> String {
    let host = host.trim_end_matches('.').to_ascii_lowercase();
    p.host_aliases
        .iter()
        .find(|(alias, _)| alias.eq_ignore_ascii_case(&host))
        .map(|(_, id)| id.trim_end_matches('.').to_ascii_lowercase())
        .unwrap_or(host)
}
pub fn same_host(a: &str, b: &str, p: &Policy) -> bool {
    let a = canonical_host(a, p);
    let b = canonical_host(b, p);
    !a.is_empty() && !b.is_empty() && (a == b || a.split('.').next() == b.split('.').next())
}

/// HTTP claim result carries its epoch through compensation; no priority writes.
pub trait Launch {
    fn claim(&mut self, key: &str) -> Result<u64, ClaimError>;
    fn start(&mut self, card: &Value, brief: &str) -> Result<Value, String>;
    /// Reserve the status file of one launch exclusively and return its path. `wanted` is the
    /// preferred path; the result is unique among every launch that shares the review
    /// directory. The default has no shared directory to arbitrate and keeps `wanted`.
    fn allocate_status(&mut self, wanted: &str) -> Result<String, String> {
        Ok(wanted.into())
    }
    fn release(&mut self, key: &str, epoch: u64) -> Result<(), String>;
    /// After `key` is claimed: the key of another claimed item (outside `batch`) whose files
    /// overlap `files`. The claimant yields to any such claim, so two hosts claiming at once
    /// never both keep overlapping files. The default has no shared registry to ask.
    fn rival(
        &mut self,
        _key: &str,
        _files: &[String],
        _batch: &[String],
    ) -> Result<Option<String>, String> {
        Ok(None)
    }
}
pub fn spawn(card: &Value, brief: &str, claim: &mut impl Launch) -> Result<Value, SpawnError> {
    let key = card["item_key"]
        .as_str()
        .ok_or_else(|| SpawnError::Start("card missing item_key".into()))?;
    let epoch = claim.claim(key).map_err(SpawnError::Claim)?;
    match claim.start(card, brief) {
        Ok(run) => Ok(run),
        Err(e) => match claim.release(key, epoch) {
            Ok(()) => Err(SpawnError::Start(format!(
                "spawn failed; claim released: {e}"
            ))),
            Err(release) => Err(SpawnError::Start(format!(
                "spawn failed: {e}; release failed: {release}"
            ))),
        },
    }
}

/// Dry run stops before the first write, including before claim and artifact creation.
pub fn dispatch(
    dry_run: bool,
    card: &Value,
    brief: &str,
    launch: &mut impl Launch,
) -> Result<Value, SpawnError> {
    if dry_run {
        Ok(json!({"decision":"would_spawn", "card":card, "brief":brief}))
    } else {
        spawn(card, brief, launch).map(decided)
    }
}
/// The row of a launched run: queued, unknown and planned receipts stay pending.
pub(crate) fn decided(run: Value) -> Value {
    let decision = if matches!(
        run["status"].as_str(),
        Some("queued" | "unknown" | "planned")
    ) {
        "pending"
    } else {
        "spawned"
    };
    json!({"decision":decision, "run":run})
}
