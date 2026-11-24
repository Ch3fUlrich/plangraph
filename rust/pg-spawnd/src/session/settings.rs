use super::{
    paths::{io_text, var, Paths},
    storage::atomic_json,
    Caps,
};
use serde_json::{json, Value};
use std::{
    fs,
    path::{Path, PathBuf},
};

pub(super) struct Settings {
    pub path: PathBuf,
    pub compact: String,
    pub generated: bool,
}

const WINDOW: &str = "CLAUDE_CODE_AUTO_COMPACT_WINDOW";
const IDLE: &str = "CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS";
const HANDOFF: &str = "AUTOOS_CTX_HANDOFF";
const HARD: &str = "AUTOOS_CTX_HARD";

fn configured_window() -> (String, i128) {
    let raw = std::env::var("FLEET_COMPACT_WINDOW").unwrap_or_else(|_| "400000".into());
    let tokens = raw.trim().parse::<i128>().unwrap_or(400000);
    (raw, tokens)
}

fn required_window(hard: u64) -> i128 {
    i128::from(hard) + 50000
}

pub(super) fn effective_window(hard: u64) -> String {
    let (raw, tokens) = configured_window();
    if required_window(hard) > tokens {
        required_window(hard).to_string()
    } else {
        raw
    }
}

pub(super) fn prepare(
    paths: &Paths,
    role: &str,
    cap: Caps,
    write: bool,
) -> Result<Settings, String> {
    let lean = paths.ops.join("fleet-settings.json");
    let (compact, base) = configured_window();
    let needed = required_window(cap.hard);
    let doc = load(&lean);
    let handoff = cap.handoff.to_string();
    let hard = cap.hard.to_string();
    let shadow = has_collector(paths, role);
    let pinned = doc.as_ref().map_or(true, |doc| {
        let env = &doc["env"];
        env[HANDOFF] == handoff
            && env[HARD] == hard
            && env[WINDOW] == compact
            && env[IDLE] == compact
    });
    if needed <= base && pinned && !shadow {
        return Ok(Settings {
            path: lean,
            compact,
            generated: false,
        });
    }
    let compact = if needed > base {
        needed.to_string()
    } else {
        compact
    };
    // A role with a collector gets its own file, rewritten at every launch, so
    // its hook neither leaks to roles with the same caps nor is lost on recycle.
    let name = if shadow {
        format!("role-{role}.json")
    } else {
        format!("caps-{handoff}-{hard}.json")
    };
    let path = paths.state.join("launch-settings").join(name);
    if write {
        let mut doc = doc?;
        if !doc.is_object() {
            return Err("invalid settings object".into());
        }
        if doc["env"].is_null()
            || doc["env"] == false
            || doc["env"] == ""
            || doc["env"] == 0
            || doc["env"].as_array().is_some_and(Vec::is_empty)
        {
            doc["env"] = json!({});
        }
        let env = doc["env"]
            .as_object_mut()
            .ok_or("invalid settings environment")?;
        for (key, value) in [
            (WINDOW, &compact),
            (IDLE, &compact),
            (HARD, &hard),
            (HANDOFF, &handoff),
        ] {
            env.insert(key.into(), json!(value));
        }
        if shadow {
            pg_worker_core::shadow_follow::register_measure_hook(&mut doc)
                .map_err(|e| e.to_string())?;
        }
        let parent = path.parent().unwrap();
        fs::create_dir_all(parent).map_err(|e| io_text(&e, parent))?;
        atomic_json(&path, &doc, &format!(".{role}-")).map_err(|e| io_text(&e, &path))?;
    }
    Ok(Settings {
        path,
        compact,
        generated: true,
    })
}

fn collector_config(paths: &Paths, role: &str) -> PathBuf {
    paths
        .home
        .join(".config/plangraph/state-shadow")
        .join(format!("{role}.json"))
}

/// A role has a state-shadow collector once `host-sync --install-env state-shadow` wrote its env file.
fn has_collector(paths: &Paths, role: &str) -> bool {
    pg_worker_core::turn_budget::safe_name(role)
        && collector_config(paths, role)
            .with_extension("env")
            .is_file()
}

/// Point the role's collector at the session just launched, so it reads the new transcript.
pub(super) fn follow_collector(paths: &Paths, role: &str, sid: &str, cwd: &str) {
    if !has_collector(paths, role) {
        return;
    }
    let config = collector_config(paths, role);
    if let Err(error) =
        pg_worker_core::shadow_follow::follow_session(&config, &paths.home, sid, Path::new(cwd))
    {
        eprintln!(
            "fleet-launch: warning: shadow config {} not updated: {error}",
            config.display()
        );
    }
}

fn load(path: &Path) -> Result<Value, String> {
    let file = fs::read(path).map_err(|e| io_text(&e, path))?;
    serde_json::from_slice(&file).map_err(|e| e.to_string())
}

pub(super) fn tool_path(home: &Path) -> String {
    let raw = var("PATH").unwrap_or_default();
    let have: Vec<_> = if raw.is_empty() {
        Vec::new()
    } else {
        raw.split(':').collect()
    };
    let mut dirs: Vec<_> = fs::read_dir(home.join(".nvm/versions/node"))
        .into_iter()
        .flatten()
        .filter_map(Result::ok)
        .filter(|entry| !entry.file_name().as_encoded_bytes().starts_with(b"."))
        .map(|entry| entry.path().join("bin"))
        .collect();
    dirs.sort_unstable_by(|a, b| b.cmp(a));
    dirs.push(home.join(".qoder/entry"));
    let mut add: Vec<_> = dirs
        .iter()
        .filter(|dir| dir.is_dir())
        .filter_map(|dir| dir.to_str())
        .filter(|dir| !have.contains(dir))
        .collect();
    add.extend(have);
    add.join(":")
}
