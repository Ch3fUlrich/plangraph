//! Host registry: one JSON file listing the fleet's hosts (plan rev 6.4, D1).
//! R1a parses and validates a registry given as a path or a string: no daemon,
//! no network, no reads of default config locations.

use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::path::Path;

/// How a worker reaches a host.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Transport {
    Local,
    Keeper,
    Ssh,
}

/// Host operating system; selects the path rules (V3, V4).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Os {
    Linux,
    Windows,
}

/// Work a host may be asked to do.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Role {
    Implement,
    Review,
    Research,
    Orchestrate,
    Session,
}

#[cfg(test)]
const HOST_ID_PATTERN: &str = "^[a-z][a-z0-9-]{0,31}$";
#[cfg(test)]
const REPO_ID_PATTERN: &str = "^[a-z0-9][a-z0-9._-]{0,63}$";
const ROLE_KEYS: [&str; 5] = ["implement", "review", "research", "orchestrate", "session"];
/// The `%SLOT%` token: one path segment, only in cache_env values (V5).
const SLOT: &str = "%SLOT%";

/// Per-host run limits (V6).
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Caps {
    pub max_runs: u64,
    pub max_writers: u64,
    pub max_seats: u64,
    pub max_sessions: u64,
    pub mem_floor_mb: u64,
}

/// Warmup seconds and per-role ramp memory (V6).
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Warmup {
    pub warmup_s: u64,
    pub ramp_mb: BTreeMap<String, u64>,
}

/// One repository checked out on a host.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RepoEntry {
    pub root: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub live_dir: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub health_checks: Option<RepoHealthChecks>,
    pub worktrees_root: String,
    pub remote: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub python: Option<String>,
    pub import_roots: Vec<String>,
    pub cache_env: BTreeMap<String, String>,
}

/// Argv commands used to verify a candidate live checkout.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RepoHealthChecks {
    pub doctor: Vec<String>,
    pub whoami: Vec<String>,
    pub spawn: Vec<String>,
}

/// Policy label; admission still uses the host's existing caps.
#[derive(Debug, Default, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum HostClass {
    #[default]
    Worker,
    Restricted,
}

/// One host in the registry.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct HostEntry {
    pub id: String,
    #[serde(default)]
    pub class: HostClass,
    pub aliases: Vec<String>,
    pub transport: Transport,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub ssh_target: Option<String>,
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub env: BTreeMap<String, String>,
    pub os: Os,
    pub state_dir: String,
    pub caps: Caps,
    pub warmup: Warmup,
    pub session_mb: u64,
    pub roles: Vec<Role>,
    pub repos: BTreeMap<String, RepoEntry>,
}

/// The whole hosts file.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct HostRegistry {
    pub version: u32,
    #[serde(rename = "self")]
    pub self_host: String,
    pub hosts: Vec<HostEntry>,
}

/// One error variant per validation rule V1-V7, plus io/parse plumbing.
#[derive(Debug, thiserror::Error)]
pub enum HostsError {
    #[error("V1 {0}")]
    VersionSelf(String),
    #[error("V2 {0}")]
    Namespace(String),
    #[error("V3 {0}")]
    AbsolutePath(String),
    #[error("V4 {0}")]
    OutOfTree(String),
    #[error("V5 {0}")]
    SlotToken(String),
    #[error("V6 {0}")]
    NumbersShape(String),
    #[error("duplicate role: {0}")]
    DuplicateRole(String),
    #[error("V7 {0}")]
    UnknownField(String),
    #[error("io: {0}")]
    Io(#[from] std::io::Error),
    #[error("parse: {0}")]
    Parse(#[from] serde_json::Error),
}

fn map_serde(err: serde_json::Error) -> HostsError {
    let msg = err.to_string();
    match msg
        .strip_prefix("unknown field `")
        .and_then(|r| r.split('`').next())
    {
        Some(field) => HostsError::UnknownField(v("-", "-", field, &msg)),
        None => HostsError::Parse(err),
    }
}

impl HostRegistry {
    /// Parse and validate a registry from its JSON text.
    pub fn from_json_str(s: &str) -> Result<HostRegistry, HostsError> {
        let value: serde_json::Value = serde_json::from_str(s)?;
        validate_fields(&value)?;
        let reg: HostRegistry = serde_json::from_str(s).map_err(map_serde)?;
        reg.validate()?;
        Ok(reg)
    }

    /// Read, parse and validate a registry file.
    pub fn load(path: &Path) -> Result<HostRegistry, HostsError> {
        Self::from_json_str(&std::fs::read_to_string(path)?)
    }

    /// The entry `self` names; valid registries always resolve it.
    pub fn self_host(&self) -> &HostEntry {
        self.resolve(&self.self_host)
            .expect("self resolves after validation")
    }

    /// Map an id or alias (any case) to its canonical entry (V2).
    pub fn resolve(&self, name: &str) -> Option<&HostEntry> {
        self.hosts.iter().find(|h| {
            h.id.to_lowercase() == name.to_lowercase()
                || h.aliases
                    .iter()
                    .any(|a| a.to_lowercase() == name.to_lowercase())
        })
    }

    /// Run every validation rule (V1-V7).
    pub fn validate(&self) -> Result<(), HostsError> {
        validate_slots(&serde_json::to_value(self)?, "-", "-", "registry")?;
        if self.version != 1 {
            return Err(HostsError::VersionSelf(v("-", "-", "version", "must be 1")));
        }
        // V2: ids and aliases share one case-insensitive namespace.
        let mut names: BTreeMap<String, &str> = BTreeMap::new();
        for h in &self.hosts {
            if !valid_id(&h.id) {
                return Err(HostsError::Namespace(v(&h.id, "-", "id", "bad host id")));
            }
            for name in std::iter::once(&h.id).chain(&h.aliases) {
                if let Some(prev) = names.insert(name.to_lowercase(), &h.id) {
                    let msg = format!("`{name}` collides with `{prev}`");
                    return Err(HostsError::Namespace(v(&h.id, "-", "id/aliases", &msg)));
                }
            }
        }
        // V1: `self` resolves to a declared host.
        if !names.contains_key(&self.self_host.to_lowercase()) {
            return Err(HostsError::VersionSelf(v(
                &self.self_host,
                "-",
                "self",
                "no such host",
            )));
        }
        let me = self.resolve(&self.self_host).unwrap().id.as_str();
        for h in &self.hosts {
            validate_host(h, h.id == me)?;
        }
        Ok(())
    }
}

impl HostEntry {
    /// The entry for one repo id.
    pub fn repo(&self, id: &str) -> Option<&RepoEntry> {
        self.repos.get(id)
    }
}

mod validate;
use validate::*;

#[cfg(test)]
mod tests;
