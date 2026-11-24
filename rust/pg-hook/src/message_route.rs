//! PreToolUse routing for `SendMessage`: routine status lines to the routing role go to its
//! inbox file instead of waking it, and a name that is no longer the live session of a
//! singleton role is refused with the live name. Every error fails open (exit 0).
use chrono::{DateTime, SecondsFormat, Utc};
use serde_json::{json, Value};
use std::fs::{self, OpenOptions};
use std::io::{BufRead, Write};
use std::path::{Path, PathBuf};

/// First words that mark a routine line; BLOCKED, GO-REQ, FAIL, INCIDENT and relays are not here.
const ROUTINE: [&str; 5] = ["ACK", "DONE", "FILED", "MERGED", "STATUS"];
/// Roles that have one live session at a time; `PG_MESSAGE_SINGLETON_ROLES` (comma list) replaces them.
const SINGLETONS: &str = "routing,plan-order,plan-roadmap";
const ROUTING_ROLE: &str = "routing";

/// `<role>-<counter>` split into the role and its counter; None for a bare role name.
fn split_counter(name: &str) -> Option<(&str, u64)> {
    let (role, counter) = name.rsplit_once('-')?;
    let valid = counter.len() >= 4 && counter.bytes().all(|b| b.is_ascii_digit());
    if valid {
        Some((role, counter.parse().ok()?))
    } else {
        None
    }
}

fn state_dir() -> Option<PathBuf> {
    if let Some(dir) = std::env::var_os("ROUTING_STATE_DIR").filter(|v| !v.is_empty()) {
        return Some(PathBuf::from(dir));
    }
    let base = std::env::var_os("XDG_STATE_HOME")
        .filter(|v| !v.is_empty())
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|home| Path::new(&home).join(".local/state")))?;
    Some(base.join("routing"))
}

/// Launch records `launch/sessions/<name>.json` that name the given role, as (counter, record).
fn launch_records(state: &Path, role: &str) -> Vec<(u64, Value)> {
    let Ok(entries) = fs::read_dir(state.join("launch/sessions")) else {
        return Vec::new();
    };
    entries
        .filter_map(Result::ok)
        .filter_map(|entry| {
            let path = entry.path();
            let stem = path.file_stem()?.to_str()?;
            let (found, counter) = split_counter(stem)?;
            (found == role && path.extension()? == "json").then_some(())?;
            let record = serde_json::from_slice(&fs::read(&path).ok()?).ok()?;
            Some((counter, record))
        })
        .collect()
}

/// The newest launched session name of a role: the highest counter in the launch records.
fn live_name(state: &Path, role: &str) -> Option<(u64, String)> {
    launch_records(state, role)
        .into_iter()
        .max_by_key(|(counter, _)| *counter)
        .map(|(counter, _)| (counter, format!("{role}-{counter:04}")))
}

/// The sender name: the launch record whose session id matches, else the id itself.
fn sender_name(state: &Path, payload: &Value) -> String {
    let id = payload["session_id"].as_str().unwrap_or("unknown");
    let Ok(entries) = fs::read_dir(state.join("launch/sessions")) else {
        return id.to_owned();
    };
    entries
        .filter_map(Result::ok)
        .filter_map(|entry| serde_json::from_slice::<Value>(&fs::read(entry.path()).ok()?).ok())
        .find(|record| record["session_id"].as_str() == Some(id))
        .and_then(|record| record["session_name"].as_str().map(str::to_owned))
        .unwrap_or_else(|| id.to_owned())
}

fn routine_word(message: &str) -> Option<&'static str> {
    let first = message.split_whitespace().next()?;
    let word = first.trim_end_matches(|c: char| !c.is_ascii_alphanumeric());
    ROUTINE.into_iter().find(|routine| *routine == word)
}

fn one_line(text: &str) -> String {
    text.split_whitespace().collect::<Vec<_>>().join(" ")
}

fn divert(
    state: &Path,
    sender: &str,
    to: &str,
    word: &str,
    message: &str,
    now: DateTime<Utc>,
) -> std::io::Result<PathBuf> {
    let dir = state.join("inbox");
    fs::create_dir_all(&dir)?;
    let stamp = now.to_rfc3339_opts(SecondsFormat::Secs, true);
    let inbox = dir.join("L0.md");
    let line = format!("- {stamp} {sender}: {}\n", one_line(message));
    OpenOptions::new()
        .create(true)
        .append(true)
        .open(&inbox)?
        .write_all(line.as_bytes())?;
    // One JSON row per diversion: the count per day and sender is read from this file.
    let row = json!({"ts": stamp, "day": &stamp[..10], "sender": sender, "to": to, "word": word});
    let mut log = OpenOptions::new()
        .create(true)
        .append(true)
        .open(dir.join("diversions.jsonl"))?;
    writeln!(log, "{row}")?;
    Ok(inbox)
}

pub fn hook(input: impl BufRead, diagnostic: &mut impl Write) -> i32 {
    match state_dir() {
        Some(state) => hook_in(input, diagnostic, &state, Utc::now()),
        None => 0,
    }
}

pub fn hook_in(
    input: impl BufRead,
    diagnostic: &mut impl Write,
    state: &Path,
    now: DateTime<Utc>,
) -> i32 {
    let Ok(payload) = serde_json::from_reader::<_, Value>(input) else {
        return 0;
    };
    if payload["hook_event_name"] != "PreToolUse" || payload["tool_name"] != "SendMessage" {
        return 0;
    }
    let (Some(to), Some(message)) = (
        payload["tool_input"]["to"].as_str(),
        payload["tool_input"]["message"].as_str(),
    ) else {
        return 0;
    };
    let (role, counter) = match split_counter(to) {
        Some((role, counter)) => (role, Some(counter)),
        None => (to, None),
    };
    let singletons =
        std::env::var("PG_MESSAGE_SINGLETON_ROLES").unwrap_or_else(|_| SINGLETONS.into());
    if !singletons.split(',').any(|known| known.trim() == role) {
        return 0;
    }
    if let (Some(counter), Some((live_counter, live))) = (counter, live_name(state, role)) {
        if live_counter > counter {
            let _ = writeln!(
                diagnostic,
                "message-route: {to} is no longer the live session of {role}; send to {live} \
                 (fleet-name {role} prints the live name)"
            );
            return 2;
        }
    }
    if role != ROUTING_ROLE {
        return 0;
    }
    let Some(word) = routine_word(message) else {
        return 0;
    };
    let sender = sender_name(state, &payload);
    match divert(state, &sender, to, word, message, now) {
        Ok(inbox) => {
            let _ = writeln!(
                diagnostic,
                "message-route: {word} line to {ROUTING_ROLE} was written to {} and not sent; \
                 routing reads it at its heartbeat, do not resend. BLOCKED, GO-REQ, FAIL and \
                 INCIDENT messages are delivered.",
                inbox.display()
            );
            2
        }
        Err(_) => 0,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn counters_split_only_with_four_digits() {
        assert_eq!(split_counter("plan-order-0745"), Some(("plan-order", 745)));
        assert_eq!(split_counter("routing"), None);
        assert_eq!(split_counter("plan-order"), None);
        assert_eq!(split_counter("writer-12"), None);
    }

    #[test]
    fn only_the_labelled_first_word_is_routine() {
        for yes in ["ACK", "DONE: x", "MERGED, y", "STATUS ok", "FILED\nmore"] {
            assert!(routine_word(yes).is_some(), "{yes}");
        }
        for no in [
            "BLOCKED x",
            "GO-REQ x",
            "FAIL",
            "INCIDENT",
            "Done now",
            "ACKNOWLEDGED",
            "",
            "x DONE",
        ] {
            assert!(routine_word(no).is_none(), "{no}");
        }
    }
}
