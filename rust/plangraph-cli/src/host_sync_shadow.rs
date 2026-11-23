//! Prepare the role environments and the Claude measurement hooks together.
use serde_json::{json, Value};
use std::{fs, io, path::Path};

const ROLES: [&str; 2] = ["plan-order", "routing"];
const EVENTS: [&str; 5] = [
    "UserPromptSubmit",
    "Notification",
    "PreToolUse",
    "PostToolUse",
    "Stop",
];
const COMMAND: &str = r#""$HOME/.local/bin/turn-budget-hook" --measure-only"#;

fn prepare(bytes: &[u8]) -> io::Result<Option<Vec<u8>>> {
    let mut document: Value = serde_json::from_slice(bytes).map_err(io::Error::other)?;
    let settings = document
        .as_object_mut()
        .ok_or_else(|| io::Error::other("expected a Claude settings object"))?;
    let hooks = settings
        .entry("hooks")
        .or_insert_with(|| json!({}))
        .as_object_mut()
        .ok_or_else(|| io::Error::other("expected a Claude hooks object"))?;
    let mut changed = false;
    for event in EVENTS {
        let groups = hooks
            .entry(event)
            .or_insert_with(|| json!([]))
            .as_array_mut()
            .ok_or_else(|| io::Error::other(format!("expected an array for {event} hooks")))?;
        if groups
            .iter()
            .filter(|group| {
                group["matcher"]
                    .as_str()
                    .is_none_or(|m| m.is_empty() || m == "*")
            })
            .filter_map(|group| group["hooks"].as_array())
            .flatten()
            .any(|hook| hook["type"] == "command" && hook["command"] == COMMAND)
        {
            continue;
        }
        groups.push(json!({
            "matcher": "",
            "hooks": [{"type": "command", "command": COMMAND, "onFailure": "continue"}]
        }));
        changed = true;
    }
    if !changed {
        return Ok(None);
    }
    let mut output = serde_json::to_vec_pretty(&document).map_err(io::Error::other)?;
    output.push(b'\n');
    Ok(Some(output))
}

pub(super) fn install(home: &Path, config: &Path, profiles: [&Path; 2]) -> io::Result<()> {
    let hook = home.join(".local/bin/turn-budget-hook");
    if !super::super::is_executable(&hook) {
        return Err(io::Error::other(format!(
            "missing executable {}",
            hook.display()
        )));
    }
    pg_worker_core::run::record_io::ensure_private_dir(config)?;
    let _lock = pg_worker_core::lockfile::try_lock(&config.join("install.lock"))
        .map_err(io::Error::other)?
        .ok_or_else(|| io::Error::other("another shadow install is running"))?;
    let mut changes = Vec::new();
    for profile in profiles {
        let profile = profile
            .canonicalize()
            .map_err(|err| io::Error::other(format!("{}: {err}", profile.display())))?;
        if let Some(bytes) = prepare(&fs::read(&profile)?)
            .map_err(|err| io::Error::other(format!("{}: {err}", profile.display())))?
        {
            changes.push((profile, bytes));
        }
    }

    for role in ROLES {
        let token = super::assignment("PG_TOKEN_FILE", &config.join(format!("{role}.token")))?;
        let source = super::assignment(
            "PLANGRAPH_STATE_CONFIG",
            &config.join(format!("{role}.json")),
        )?;
        super::create(
            &config.join(format!("{role}.env")),
            format!("{token}{source}").as_bytes(),
        )?;
    }
    for (profile, bytes) in changes {
        pg_worker_core::run::record_io::atomic_write_private(&profile, &bytes)?;
        println!("REGISTERED {}", profile.display());
    }
    Ok(())
}
