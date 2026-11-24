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

/// The logical lines of an environment file: a comment line (`#` or `;`) ends at its line break
/// whatever it ends with; any other line ending in an odd number of backslashes is continued by
/// the next line, the backslash and the line break dropped (as systemd reads it).
fn logical_lines(text: &str) -> Vec<String> {
    let mut lines = Vec::new();
    let mut current: Option<String> = None;
    for line in text.lines() {
        if current.is_none() && line.trim_start().starts_with(['#', ';']) {
            continue;
        }
        let mut joined = current.take().unwrap_or_default();
        joined.push_str(line);
        let slashes = joined.bytes().rev().take_while(|c| *c == b'\\').count();
        if slashes % 2 == 1 {
            joined.pop();
            current = Some(joined);
        } else {
            lines.push(joined);
        }
    }
    lines.extend(current);
    lines
}

/// Resolve backslash escapes. With `trim`, whitespace left unescaped at the end is dropped (an
/// unquoted value); a quoted value keeps every space inside its quotes.
fn unescape(text: &str, trim: bool) -> Option<String> {
    let mut out: Vec<(char, bool)> = Vec::new();
    let mut chars = text.chars();
    while let Some(ch) = chars.next() {
        match ch {
            '\\' => out.push((chars.next()?, true)),
            other => out.push((other, false)),
        }
    }
    while trim
        && out
            .last()
            .is_some_and(|(c, escaped)| !escaped && c.is_whitespace())
    {
        out.pop();
    }
    Some(out.into_iter().map(|(c, _)| c).collect())
}

/// The value of an environment-file assignment in systemd syntax: double-quoted (every character
/// inside the quotes is kept, escapes resolved), single-quoted literally, or unquoted with
/// backslash escapes (`\ ` is a space that stays, also at the end). Only unescaped whitespace
/// around the value is dropped.
fn env_value(raw: &str) -> Option<String> {
    let raw = raw.trim_start();
    if let Some(inner) = raw.strip_prefix('"') {
        return unescape(inner.trim_end().strip_suffix('"')?, false);
    }
    if let Some(inner) = raw.strip_prefix('\'') {
        return inner.trim_end().strip_suffix('\'').map(str::to_owned);
    }
    unescape(raw, true).filter(|value| !value.is_empty())
}

/// The value of the `name` assignment on one environment-file line; whitespace around the name
/// and the `=` is allowed, as for the service.
fn assignment<'a>(line: &'a str, name: &str) -> Option<&'a str> {
    let (key, value) = line.split_once('=')?;
    (key.trim() == name).then_some(value)
}

/// The role's collector configuration: the `PLANGRAPH_STATE_CONFIG` its `ROLE.env` sets (the last
/// assignment wins, as for the service), else the default `ROLE.json` beside it. The launcher and
/// the collector must act on the same file.
pub fn config_path(home: &Path, role: &str) -> PathBuf {
    let dir = home.join(".config/plangraph/state-shadow");
    fs::read_to_string(dir.join(format!("{role}.env")))
        .ok()
        .and_then(|text| {
            logical_lines(&text)
                .iter()
                .filter_map(|line| assignment(line, "PLANGRAPH_STATE_CONFIG").and_then(env_value))
                .next_back()
        })
        .map_or_else(|| dir.join(format!("{role}.json")), PathBuf::from)
}

/// The session the role's committed launch record names (the launcher writes it, and the watchdog
/// updates it when it adopts a replacement later) with its working directory.
pub fn launch_session(state_dir: &Path, role: &str) -> Option<(String, PathBuf)> {
    let record: Value = serde_json::from_slice(
        &fs::read(state_dir.join("launch").join(format!("{role}.json"))).ok()?,
    )
    .ok()?;
    let session = record["session_id"]
        .as_str()
        .filter(|id| crate::turn_budget::safe_name(id))?;
    Some((session.to_owned(), PathBuf::from(record["cwd"].as_str()?)))
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
    fn config_path_follows_the_env_file_assignment() {
        let home = tempfile::tempdir().unwrap();
        let dir = home.path().join(".config/plangraph/state-shadow");
        fs::create_dir_all(&dir).unwrap();
        assert_eq!(
            config_path(home.path(), "routing"),
            dir.join("routing.json")
        );
        fs::write(
            dir.join("routing.env"),
            "PG_TOKEN_FILE=\"/t\"\nPLANGRAPH_STATE_CONFIG=\"/x/my \\\"cfg\\\".json\"\n",
        )
        .unwrap();
        assert_eq!(
            config_path(home.path(), "routing"),
            PathBuf::from("/x/my \"cfg\".json")
        );
    }

    #[test]
    fn config_path_honors_unquoted_single_quoted_and_the_last_assignment() {
        let home = tempfile::tempdir().unwrap();
        let dir = home.path().join(".config/plangraph/state-shadow");
        fs::create_dir_all(&dir).unwrap();
        let env = dir.join("routing.env");
        for (text, want) in [
            ("PLANGRAPH_STATE_CONFIG=/p/custom.json\n", "/p/custom.json"),
            (
                "PLANGRAPH_STATE_CONFIG=/p/with space.json  \n",
                "/p/with space.json",
            ),
            (
                "PLANGRAPH_STATE_CONFIG=/p/with\\ space.json\n",
                "/p/with space.json",
            ),
            (
                "PLANGRAPH_STATE_CONFIG = /p/spaced.json\n",
                "/p/spaced.json",
            ),
            ("  PLANGRAPH_STATE_CONFIG =\"/p/q.json\"  \n", "/p/q.json"),
            (
                "PLANGRAPH_STATE_CONFIG=/p/cont\\\ninued.json\n",
                "/p/continued.json",
            ),
            (
                "PLANGRAPH_STATE_CONFIG=/p/ends-with-space\\ \n",
                "/p/ends-with-space ",
            ),
            (
                "PLANGRAPH_STATE_CONFIG=/p/two\\\\\nPLANGRAPH_STATE_CONFIG=/p/last.json\n",
                "/p/last.json",
            ),
            (
                "# a note ending in a backslash \\\nPLANGRAPH_STATE_CONFIG=/p/after-comment.json\n",
                "/p/after-comment.json",
            ),
            (
                "PLANGRAPH_STATE_CONFIG=\"/p/quoted-space.json \"\n",
                "/p/quoted-space.json ",
            ),
            (
                "; comment \\\n  # another\nPLANGRAPH_STATE_CONFIG=/p/c.json\n",
                "/p/c.json",
            ),
            (
                "PLANGRAPH_STATE_CONFIG='/p/lit$eral.json'\n",
                "/p/lit$eral.json",
            ),
            (
                "PLANGRAPH_STATE_CONFIG=/p/first.json\nPLANGRAPH_STATE_CONFIG=\"/p/last.json\"\n",
                "/p/last.json",
            ),
        ] {
            fs::write(&env, text).unwrap();
            assert_eq!(
                config_path(home.path(), "routing"),
                PathBuf::from(want),
                "{text}"
            );
        }
    }

    #[test]
    fn launch_session_reads_the_committed_record() {
        let state = tempfile::tempdir().unwrap();
        assert!(launch_session(state.path(), "routing").is_none());
        fs::create_dir_all(state.path().join("launch")).unwrap();
        let record = state.path().join("launch/routing.json");
        fs::write(&record, r#"{"session_id":null,"cwd":"/w"}"#).unwrap();
        assert!(launch_session(state.path(), "routing").is_none());
        fs::write(&record, r#"{"session_id":"sid-2","cwd":"/w2"}"#).unwrap();
        assert_eq!(
            launch_session(state.path(), "routing"),
            Some(("sid-2".into(), PathBuf::from("/w2")))
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
