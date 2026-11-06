//! Adapter boundary only: durable keeper transactions are an integration dependency.
use crate::placement_rules::{resolve, schema, RuleLayers};
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
    match store.read() {
        Ok(snapshot) => {
            validate_snapshot(&snapshot)?;
            replace_cache(path, &snapshot)?;
            Ok(snapshot)
        }
        Err(StoreError::Unavailable) => read_cache(path).map_err(|_| StoreError::Unavailable),
        Err(error) => Err(error),
    }
}

#[cfg(test)]
#[path = "placement_rule_store_tests.rs"]
mod tests;
