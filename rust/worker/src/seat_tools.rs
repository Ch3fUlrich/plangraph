//! The read-only tool policy of an OpenCode review seat: render it, prove an arbitrary config
//! matches it, and record the denials of a run.
//!
//! A seat may read the checkout and run the commands in [`CHECKOUT_COMMANDS`]; every other tool is
//! denied, `external_directory` and writes included. The proof fails closed: a tool outside the
//! policy that carries any value but `deny` (a new key, an extra bash allow, a non-empty `mcp` or
//! `plugin`) refuses the seat instead of being ignored.

use crate::opencode_events;
use pg_worker_core::hosts::HostRegistry;
use serde_json::{Map, Value, json};

/// Every tool key the OpenCode config schema knows; all start denied.
pub const TOOLS: [&str; 15] = [
    "bash",
    "doom_loop",
    "edit",
    "external_directory",
    "glob",
    "grep",
    "list",
    "lsp",
    "question",
    "read",
    "skill",
    "task",
    "todowrite",
    "webfetch",
    "websearch",
];

/// Tools a seat may use to read the checkout.
pub const READ_TOOLS: [&str; 4] = ["read", "glob", "grep", "list"];

/// The shell commands a seat may run inside its checkout.
pub const CHECKOUT_COMMANDS: [&str; 9] = [
    "git diff *",
    "git show *",
    "git status *",
    "git log *",
    "git rev-parse *",
    "cargo test *",
    "pytest *",
    "python -m pytest *",
    "python3 -m pytest *",
];

/// Whether the reviewed repository is public, by the hosts registry's `public_repos`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Privacy {
    Public,
    Private,
}

impl Privacy {
    /// Public only for a repo id the hosts registry lists in `public_repos`; any other id,
    /// including one the registry does not know, is private, so sharing stays disabled.
    pub fn of_repo(registry: &HostRegistry, repo_id: &str) -> Privacy {
        if registry.public_repos.contains(repo_id) {
            Privacy::Public
        } else {
            Privacy::Private
        }
    }
}

/// The config a seat runs with. A private repository also turns session sharing off.
pub fn render(privacy: Privacy) -> Value {
    let mut permission = Map::new();
    permission.insert("*".into(), json!("deny"));
    for tool in TOOLS {
        permission.insert(tool.into(), json!("deny"));
    }
    for tool in READ_TOOLS {
        permission.insert(tool.into(), json!("allow"));
    }
    let mut bash = Map::new();
    bash.insert("*".into(), json!("deny"));
    for command in CHECKOUT_COMMANDS {
        bash.insert(command.into(), json!("allow"));
    }
    permission.insert("bash".into(), Value::Object(bash));
    let mut config = json!({"permission": permission, "mcp": {}, "plugin": []});
    if privacy == Privacy::Private {
        config["share"] = json!("disabled");
    }
    config
}

/// A config that does not match the seat policy.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Refusal {
    /// The config has no `permission` object.
    NoPermission,
    /// A tool outside the policy is allowed (or asks), or a policy tool is missing.
    UnexpectedAllow(String),
    /// `mcp` is not `{}` or `plugin` is not `[]`.
    ExtraSurface(&'static str),
    /// A private repository's config does not disable sharing.
    SharingEnabled,
}

/// Proves `config` allows exactly what [`render`] allows for `privacy`.
pub fn check(config: &Value, privacy: Privacy) -> Result<(), Refusal> {
    let permission = config
        .get("permission")
        .and_then(Value::as_object)
        .ok_or(Refusal::NoPermission)?;
    for tool in TOOLS.into_iter().chain(["*"]) {
        if !permission.contains_key(tool) {
            return Err(Refusal::UnexpectedAllow(format!("{tool}: missing")));
        }
    }
    for (tool, value) in permission {
        let ok = match tool.as_str() {
            t if READ_TOOLS.contains(&t) => matches!(value.as_str(), Some("allow" | "deny")),
            "bash" => bash_is_scoped(value),
            _ => value.as_str() == Some("deny"),
        };
        if !ok {
            return Err(Refusal::UnexpectedAllow(tool.clone()));
        }
    }
    if config.get("mcp") != Some(&json!({})) {
        return Err(Refusal::ExtraSurface("mcp"));
    }
    if config.get("plugin") != Some(&json!([])) {
        return Err(Refusal::ExtraSurface("plugin"));
    }
    if privacy == Privacy::Private && config.get("share") != Some(&json!("disabled")) {
        return Err(Refusal::SharingEnabled);
    }
    Ok(())
}

fn bash_is_scoped(value: &Value) -> bool {
    let Some(rules) = value.as_object() else {
        return value.as_str() == Some("deny");
    };
    rules.get("*").and_then(Value::as_str) == Some("deny")
        && rules.iter().all(|(command, effect)| match effect.as_str() {
            Some("deny") => true,
            Some("allow") => CHECKOUT_COMMANDS.contains(&command.as_str()),
            _ => false,
        })
}

/// The `permission_denied` record of a run, from its event stream; `None` when nothing was refused.
pub fn record_denials(stream: &str) -> Option<Value> {
    let count = opencode_events::summarize(stream).permission_denied;
    (count > 0).then(|| json!({"event": "permission_denied", "count": count}))
}

#[cfg(test)]
#[path = "seat_tools_tests.rs"]
mod tests;
