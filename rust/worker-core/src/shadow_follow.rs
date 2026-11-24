//! Keep a state-shadow role attached to its live Claude session.
//!
//! The collector's `ROLE.json` names the session transcript, and the role's
//! Claude settings carry the measure-only hook. Both go stale when the fleet
//! recycles the session: a new session id, possibly under another project
//! directory. The launcher and `host-sync` call this module so there is one
//! implementation of each rule.
use serde_json::{json, Value};
use std::{fs, io, path::Path, path::PathBuf};

pub const MEASURE_COMMAND: &str = r#""$HOME/.local/bin/turn-budget-hook" --measure-only"#;
const EVENTS: [&str; 5] = [
    "UserPromptSubmit",
    "Notification",
    "PreToolUse",
    "PostToolUse",
    "Stop",
];

/// Add the measure-only hook to every event of a Claude settings document.
/// Returns whether the document changed; existing hooks are kept.
pub fn register_measure_hook(document: &mut Value) -> io::Result<bool> {
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
            .any(|hook| hook["type"] == "command" && hook["command"] == MEASURE_COMMAND)
        {
            continue;
        }
        groups.push(json!({
            "matcher": "",
            "hooks": [{"type": "command", "command": MEASURE_COMMAND, "onFailure": "continue"}]
        }));
        changed = true;
    }
    Ok(changed)
}

/// The existing transcript of `session_id` under any Claude project directory.
pub fn find_transcript(home: &Path, session_id: &str) -> Option<PathBuf> {
    let name = format!("{session_id}.jsonl");
    let mut found: Vec<PathBuf> = fs::read_dir(home.join(".claude/projects"))
        .into_iter()
        .flatten()
        .filter_map(Result::ok)
        .map(|entry| entry.path().join(&name))
        .filter(|path| path.is_file())
        .collect();
    found.sort();
    found.pop()
}

/// Where Claude keeps the transcript of `session_id`: the existing file under
/// any project directory, else the directory derived from `cwd`.
pub fn transcript_for(home: &Path, session_id: &str, cwd: &Path) -> PathBuf {
    find_transcript(home, session_id).unwrap_or_else(|| {
        let project: String = cwd
            .to_string_lossy()
            .chars()
            .map(|ch| if ch.is_ascii_alphanumeric() { ch } else { '-' })
            .collect();
        home.join(".claude/projects")
            .join(project)
            .join(format!("{session_id}.jsonl"))
    })
}

/// Point the role configuration at the live session. Returns false when the
/// role has no configuration or it already names this session.
pub fn follow_session(
    config: &Path,
    home: &Path,
    session_id: &str,
    cwd: &Path,
) -> io::Result<bool> {
    if !crate::turn_budget::safe_name(session_id) {
        return Err(io::Error::other("invalid session id"));
    }
    let bytes = match fs::read(config) {
        Ok(bytes) => bytes,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(false),
        Err(error) => return Err(error),
    };
    let mut document: Value = serde_json::from_slice(&bytes).map_err(io::Error::other)?;
    let object = document
        .as_object_mut()
        .ok_or_else(|| io::Error::other("expected a shadow configuration object"))?;
    let transcript = transcript_for(home, session_id, cwd);
    if object.get("session_id") == Some(&json!(session_id))
        && object.get("transcript") == Some(&json!(transcript))
    {
        return Ok(false);
    }
    object.insert("session_id".into(), json!(session_id));
    object.insert("transcript".into(), json!(transcript));
    let mut output = serde_json::to_vec_pretty(&document).map_err(io::Error::other)?;
    output.push(b'\n');
    crate::run::record_io::atomic_write_private(config, &output)?;
    Ok(true)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hook_registration_is_idempotent_and_keeps_existing_hooks() {
        let mut doc = json!({"hooks": {"Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "keep"}]}]}});
        assert!(register_measure_hook(&mut doc).unwrap());
        assert_eq!(doc["hooks"]["Stop"].as_array().unwrap().len(), 2);
        assert_eq!(doc["hooks"]["Stop"][0]["hooks"][0]["command"], "keep");
        assert!(!register_measure_hook(&mut doc).unwrap());
        assert!(register_measure_hook(&mut json!([])).is_err());
    }

    #[test]
    fn transcript_is_found_under_any_project_directory() {
        let home = tempfile::tempdir().unwrap();
        let moved = home.path().join(".claude/projects/-moved-worktree");
        fs::create_dir_all(&moved).unwrap();
        fs::write(moved.join("abc-1.jsonl"), "").unwrap();
        assert_eq!(
            transcript_for(home.path(), "abc-1", Path::new("/other/place")),
            moved.join("abc-1.jsonl")
        );
        assert_eq!(
            transcript_for(home.path(), "zzz-2", Path::new("/a/.b/c_d")),
            home.path().join(".claude/projects/-a--b-c-d/zzz-2.jsonl")
        );
    }

    #[test]
    fn follow_rewrites_session_and_transcript_and_keeps_operator_fields() {
        let home = tempfile::tempdir().unwrap();
        let config = home.path().join("routing.json");
        assert!(!follow_session(&config, home.path(), "new-1", Path::new("/w")).unwrap());
        fs::write(
            &config,
            r#"{"role":"routing","session_id":"old-0","transcript":"/x/old-0.jsonl","principal":"p"}"#,
        )
        .unwrap();
        assert!(follow_session(&config, home.path(), "new-1", Path::new("/w")).unwrap());
        let doc: Value = serde_json::from_slice(&fs::read(&config).unwrap()).unwrap();
        assert_eq!(doc["session_id"], "new-1");
        assert_eq!(doc["principal"], "p");
        assert!(doc["transcript"]
            .as_str()
            .unwrap()
            .ends_with("/-w/new-1.jsonl"));
        assert!(!follow_session(&config, home.path(), "new-1", Path::new("/w")).unwrap());
        assert!(follow_session(&config, home.path(), "../x", Path::new("/w")).is_err());
    }
}
