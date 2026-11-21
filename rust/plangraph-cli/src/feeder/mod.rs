//! Timer-driven activity boundaries. Selection/admission/briefs are pure;
//! spawn is an effectful activity behind `Launch` (including compensating release).
mod adapters;
mod quota;
mod runtime;
mod selection;
mod sources;
mod workflow;
pub use adapters::{Env, SlowKeeper, Transport};
pub use quota::run_status as quota_status;
pub use runtime::run;
pub use selection::{decode_task, pick, prepare_ready, validate_ready};
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

#[derive(Clone, Debug, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Policy {
    pub max_writers: BTreeMap<String, usize>,
    pub host_aliases: BTreeMap<String, String>,
    pub quota_max_age_seconds: i64,
    pub ram_per_run_bytes: u64,
    pub disk_floor_bytes: u64,
    pub quota_floor_percent: f64,
    pub facts_max_age_seconds: i64,
    pub estimate_tokens: u64,
}
impl Default for Policy {
    fn default() -> Self {
        Self {
            max_writers: BTreeMap::from([("coding".into(), 3), ("dell".into(), 5)]),
            host_aliases: BTreeMap::new(),
            quota_max_age_seconds: 10_800,
            ram_per_run_bytes: 4_000_000_000,
            disk_floor_bytes: 10_000_000_000,
            quota_floor_percent: 10.0,
            facts_max_age_seconds: 300,
            estimate_tokens: 100_000,
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
}
impl Run {
    pub fn active(&self) -> bool {
        !matches!(
            self.status.as_str(),
            "completed"
                | "failed"
                | "cancelled"
                | "killed"
                | "dry_run"
                | "succeeded"
                | "timeout"
                | "timed_out"
                | "stalled"
                | "terminated_after_answer"
                | "refused"
                | "skipped"
        )
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
    // Explicit full names in private policies win; public defaults use short aliases.
    let host = canonical_host(&f.host, p);
    let limit = p
        .max_writers
        .get(&f.host)
        .or_else(|| p.max_writers.get(&host))
        .or_else(|| p.max_writers.get(host.split('.').next().unwrap_or(&host)));
    let reason = if f.kill_switch {
        Some("kill_switch")
    } else if !p.quota_floor_percent.is_finite()
        || !(0.0..100.0).contains(&p.quota_floor_percent)
        || p.facts_max_age_seconds < 0
        || p.quota_max_age_seconds < 0
        || p.ram_per_run_bytes == 0
        || p.disk_floor_bytes == 0
        || p.estimate_tokens == 0
    {
        Some("invalid_policy")
    } else if f.observed_at > f.now || f.now.saturating_sub(f.observed_at) > p.facts_max_age_seconds
    {
        Some("stale_facts")
    } else if limit.is_none() {
        Some("unknown_host")
    } else if f.running_writers >= *limit.unwrap_or(&0) {
        Some("max_writers")
    } else if f.free_ram_bytes < p.ram_per_run_bytes {
        Some("ram")
    } else if f.free_disk_bytes < p.disk_floor_bytes {
        Some("disk")
    } else if !f
        .quota_remaining_percent
        .is_some_and(|q| q.is_finite() && q > p.quota_floor_percent && q <= 100.0)
    {
        Some("provider_quota")
    } else {
        None
    };
    reason.map_or(Admission::Admit, |r| Admission::Refuse(r.into()))
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
        item.key,item.title,item.description.as_deref().unwrap_or(""),done,item.files.join("\n"),
        item.fields.get("note").and_then(Value::as_str).unwrap_or(""),serde_json::to_string(&metadata).expect("JSON values"),
        house_rules.replace("{{item_key}}",&item.key).trim())
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
    fn release(&mut self, key: &str, epoch: u64) -> Result<(), String>;
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
        spawn(card, brief, launch).map(|run| {
            let decision = if matches!(
                run["status"].as_str(),
                Some("queued" | "unknown" | "planned")
            ) {
                "pending"
            } else {
                "spawned"
            };
            json!({"decision":decision, "run":run})
        })
    }
}
