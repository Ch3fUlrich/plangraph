//! Adapter boundary only: durable keeper transactions are an integration dependency.
use crate::placement_rules::{parse_env_file, resolve, schema, RuleLayers, RuleReport};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::fs::{self, OpenOptions};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub enum RuleScope {
    Fleet,
    Host(String),
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RuleEdit {
    pub key: String,
    pub value: Option<String>,
    pub scope: RuleScope,
    pub expected_revision: u64,
    pub rollback_of: Option<u64>,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RuleAudit {
    pub revision: u64,
    pub actor: String,
    pub edit: RuleEdit,
    pub previous: Option<String>,
}
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RuleSnapshot {
    pub revision: u64,
    pub fleet: BTreeMap<String, String>,
    pub hosts: BTreeMap<String, BTreeMap<String, String>>,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq, thiserror::Error)]
pub enum StoreError {
    #[error("rules_unavailable")]
    Unavailable,
    #[error("rules_conflict")]
    Conflict,
    #[error("rules_invalid")]
    Invalid,
    #[error("rules_unauthorized")]
    Unauthorized,
    #[error("rules_io")]
    Io,
}
pub trait RuleStore {
    fn read(&self) -> Result<RuleSnapshot, StoreError>;
    /// In ONE durable transaction: authenticate actor, CAS expected_revision,
    /// reject absent deletions, validate rollback target scope/key/prior value,
    /// increment revision and write value plus immutable audit row together.
    /// Stale/duplicate requests conflict; failure writes neither. Rollback is
    /// a NEW event, never a history rewrite. Revalidate schema for direct callers.
    fn commit(&mut self, actor: &str, edit: &RuleEdit) -> Result<RuleAudit, StoreError>;
}

pub fn validate_override(
    scope: &RuleScope,
    key: &str,
    value: Option<&str>,
) -> Result<(), StoreError> {
    if matches!(scope, RuleScope::Host(host) if host.trim().is_empty() || host.len() > 64 || host.chars().any(char::is_control))
    {
        return Err(StoreError::Invalid);
    }
    let specs = schema().map_err(|_| StoreError::Invalid)?;
    let mut layers = RuleLayers::default();
    let layer = match scope {
        RuleScope::Fleet => &mut layers.keeper_fleet,
        RuleScope::Host(_) => &mut layers.keeper_host,
    };
    // Resolve through A even for deletion, using the schema default as probe.
    let defaults = resolve(&specs, "", &RuleLayers::default());
    let default = defaults.values.get(key).ok_or(StoreError::Invalid)?;
    layer.insert(
        key.into(),
        value.map(str::to_owned).unwrap_or(default.to_string()),
    );
    if resolve(&specs, "", &layers).diagnostics.is_empty() {
        Ok(())
    } else {
        Err(StoreError::Invalid)
    }
}
/// Actor is an authenticated adapter identity, never a request-body field.
pub fn edit(
    store: &mut impl RuleStore,
    actor: &str,
    request: RuleEdit,
) -> Result<RuleAudit, StoreError> {
    if actor.trim().is_empty() {
        return Err(StoreError::Unauthorized);
    }
    validate_override(&request.scope, &request.key, request.value.as_deref())?;
    store.commit(actor, &request)
}
pub(crate) fn validate_snapshot(snapshot: &RuleSnapshot) -> Result<(), StoreError> {
    if snapshot.revision > i64::MAX as u64 {
        return Err(StoreError::Invalid);
    }
    for (key, value) in &snapshot.fleet {
        validate_override(&RuleScope::Fleet, key, Some(value))?;
    }
    for (host, rules) in &snapshot.hosts {
        if host.trim().is_empty() || host.len() > 64 || host.chars().any(char::is_control) {
            return Err(StoreError::Invalid);
        }
        for (key, value) in rules {
            validate_override(&RuleScope::Host(host.clone()), key, Some(value))?;
        }
    }
    Ok(())
}
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Cache {
    version: u8,
    snapshot: RuleSnapshot,
}
const MAX_CACHE: u64 = 1024 * 1024;
fn sibling(path: &Path, suffix: &str) -> PathBuf {
    let mut name = path.as_os_str().to_owned();
    name.push(suffix);
    PathBuf::from(name)
}
fn regular(path: &Path) -> Result<(), StoreError> {
    match fs::symlink_metadata(path) {
        Ok(m) if m.is_file() && m.len() <= MAX_CACHE => Ok(()),
        _ => Err(StoreError::Io),
    }
}
fn read_cache(path: &Path) -> Result<RuleSnapshot, StoreError> {
    regular(path)?;
    let mut bytes = Vec::new();
    fs::File::open(path)
        .map_err(|_| StoreError::Io)?
        .take(MAX_CACHE + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| StoreError::Io)?;
    if bytes.len() as u64 > MAX_CACHE {
        return Err(StoreError::Invalid);
    }
    let cache: Cache = serde_json::from_slice(&bytes).map_err(|_| StoreError::Invalid)?;
    if cache.version != 1 {
        return Err(StoreError::Invalid);
    }
    validate_snapshot(&cache.snapshot)?;
    Ok(cache.snapshot)
}
fn replace_cache(path: &Path, snapshot: &RuleSnapshot) -> Result<(), StoreError> {
    let lock = sibling(path, ".lock");
    if fs::symlink_metadata(&lock).is_ok() {
        regular(&lock)?;
    }
    let _guard = crate::lockfile::try_lock(&lock)
        .map_err(|_| StoreError::Io)?
        .ok_or(StoreError::Io)?;
    if fs::symlink_metadata(path).is_ok() {
        regular(path)?;
    }
    let bytes = serde_json::to_vec(&Cache {
        version: 1,
        snapshot: snapshot.clone(),
    })
    .map_err(|_| StoreError::Io)?;
    if bytes.len() as u64 > MAX_CACHE {
        return Err(StoreError::Io);
    }
    let temp = sibling(path, &format!(".{}.tmp", std::process::id()));
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let mut file = options.open(&temp).map_err(|_| StoreError::Io)?;
    let result = (|| {
        file.write_all(&bytes)?;
        file.sync_all()?;
        fs::rename(&temp, path)
    })();
    if result.is_err() {
        let _ = fs::remove_file(&temp);
    }
    result.map_err(|_| StoreError::Io)
}
/// Only Unavailable enables LKG. Cache contains keeper layers, never process/env.
/// Adapter exposes online/offline source and revision; offline LKG has no expiry.
/// Cache directory must be private to the adapter, not writable by other users.
pub fn load_cached(store: &impl RuleStore, path: &Path) -> Result<RuleSnapshot, StoreError> {
    load_snapshot(store, path).map(|(snapshot, _)| snapshot)
}
fn load_snapshot(
    store: &impl RuleStore,
    path: &Path,
) -> Result<(RuleSnapshot, &'static str), StoreError> {
    match store.read() {
        Ok(snapshot) => {
            validate_snapshot(&snapshot)?;
            if replace_cache(path, &snapshot).is_err() {
                eprintln!("placement_rules cache_write_failed");
            }
            Ok((snapshot, "keeper"))
        }
        Err(StoreError::Unavailable) => read_cache(path)
            .map(|snapshot| (snapshot, "lkg"))
            .map_err(|_| StoreError::Unavailable),
        Err(error) => Err(error),
    }
}

/// Local configuration only; host class must come from the trusted registry.
pub struct RuleSource {
    pub cache_path: PathBuf,
    pub env_file: Option<PathBuf>,
    pub host_id: String,
    pub host_class: String,
}
pub struct LoadedRules {
    pub report: RuleReport,
    pub revision: u64,
    pub source: &'static str,
}
fn filter_process_env(
    entries: impl IntoIterator<Item = (std::ffi::OsString, std::ffi::OsString)>,
) -> (BTreeMap<String, String>, Vec<String>) {
    let mut process = BTreeMap::new();
    let mut diagnostics = Vec::new();
    for (key, value) in entries {
        if !key.as_encoded_bytes().starts_with(b"PG_RULE_") {
            continue;
        }
        match (key.into_string(), value.into_string()) {
            (Ok(key), Ok(value)) => {
                process.insert(key, value);
            }
            (Err(_), _) => diagnostics.push("PG_RULE_<unknown> process invalid_key_utf8".into()),
            (_, Err(_)) => diagnostics.push("PG_RULE_<unknown> process invalid_value_utf8".into()),
        }
    }
    (process, diagnostics)
}
impl RuleSource {
    /// Read the current process environment on every call, never from the cache.
    pub fn load(&self, store: &impl RuleStore) -> Result<LoadedRules, StoreError> {
        let (process, diagnostics) = filter_process_env(std::env::vars_os());
        let mut loaded = self.load_with_env(store, &process)?;
        loaded.report.diagnostics.extend(diagnostics);
        Ok(loaded)
    }

    /// Injectable process snapshot for hermetic callers; unrelated keys are ignored.
    pub fn load_with_env(
        &self,
        store: &impl RuleStore,
        process: &BTreeMap<String, String>,
    ) -> Result<LoadedRules, StoreError> {
        let specs = schema().map_err(|_| StoreError::Invalid)?;
        let (snapshot, source) = load_snapshot(store, &self.cache_path)?;
        let mut diagnostics = Vec::new();
        let mut env_file = BTreeMap::new();
        if let Some(path) = &self.env_file {
            match fs::read(path) {
                Ok(bytes) => {
                    for (index, bytes) in bytes.split(|b| *b == b'\n').enumerate() {
                        let bytes = bytes.strip_suffix(b"\r").unwrap_or(bytes);
                        let line = match std::str::from_utf8(bytes) {
                            Ok(line) => line,
                            Err(_) => {
                                diagnostics
                                    .push(format!("env_file line {} invalid_utf8", index + 1));
                                continue;
                            }
                        };
                        match parse_env_file(line) {
                            Ok(entries) => {
                                for (key, value) in entries {
                                    if key.starts_with("PG_RULE_") {
                                        if let std::collections::btree_map::Entry::Vacant(entry) =
                                            env_file.entry(key)
                                        {
                                            entry.insert(value);
                                        } else {
                                            diagnostics.push(format!(
                                                "env_file line {} duplicate_key",
                                                index + 1
                                            ));
                                        }
                                    }
                                }
                            }
                            Err(_) => {
                                diagnostics.push(format!("env_file line {} malformed", index + 1))
                            }
                        }
                    }
                }
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
                Err(_) => diagnostics.push("env_file unreadable".into()),
            }
        }
        let layers = RuleLayers {
            process: process
                .iter()
                .filter(|(k, _)| k.starts_with("PG_RULE_"))
                .map(|(k, v)| (k.clone(), v.clone()))
                .collect(),
            env_file,
            keeper_host: snapshot
                .hosts
                .get(&self.host_id)
                .cloned()
                .unwrap_or_default(),
            keeper_fleet: snapshot.fleet,
        };
        let mut report = resolve(&specs, &self.host_class, &layers);
        report.diagnostics.extend(diagnostics);
        Ok(LoadedRules {
            report,
            revision: snapshot.revision,
            source,
        })
    }
}

#[cfg(test)]
#[path = "placement_rule_store_tests.rs"]
mod tests;

#[cfg(test)]
#[path = "placement_rule_source_tests.rs"]
mod source_tests;
