//! PreToolUse for `SendMessage`: refuses a message that states a time other than the current UTC
//! time, and sends routine status lines to the routing role into the inbox file the router reads
//! instead of waking it, both on one clock reading. Every error fails open (exit 0).
use crate::message_time;
use chrono::{DateTime, SecondsFormat, Utc};
use serde_json::{json, Value};
use std::fs::{self, OpenOptions};
use std::io::{BufRead, Write};
use std::path::{Path, PathBuf};

/// First words that mark a routine line; BLOCKED, GO-REQ, FAIL, INCIDENT and relays are not here.
const ROUTINE: [&str; 5] = ["ACK", "DONE", "FILED", "MERGED", "STATUS"];
const ROUTING_ROLE: &str = "routing";

/// The routing state dir, as `fleet-name` finds it: `ROUTING_STATE_DIR`, else `<state>/routing`.
/// It holds the launch records the sender name comes from, not the inbox.
fn state_dir() -> Option<PathBuf> {
    match std::env::var_os("ROUTING_STATE_DIR").filter(|v| !v.is_empty()) {
        Some(dir) => Some(PathBuf::from(dir)),
        None => Some(home_state()?.join("routing")),
    }
}

/// `$XDG_STATE_HOME`, else `~/.local/state`.
fn home_state() -> Option<PathBuf> {
    std::env::var_os("XDG_STATE_HOME")
        .filter(|v| !v.is_empty())
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|home| Path::new(&home).join(".local/state")))
}

/// The inbox directory the router heartbeat reads, as `routerctl` finds it: `ROUTER_INBOX_DIR`,
/// else `<XDG state home>/routing/inbox`, whatever `ROUTING_STATE_DIR` says.
fn inbox_dir() -> Option<PathBuf> {
    match std::env::var_os("ROUTER_INBOX_DIR").filter(|v| !v.is_empty()) {
        Some(dir) => Some(PathBuf::from(dir)),
        None => Some(home_state()?.join("routing/inbox")),
    }
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

/// `routing` for `routing` and `routing-NNNN`; any other name unchanged.
fn role_of(name: &str) -> &str {
    match name.rsplit_once('-') {
        Some((role, counter))
            if counter.len() >= 4 && counter.bytes().all(|b| b.is_ascii_digit()) =>
        {
            role
        }
        _ => name,
    }
}

/// The `L0.md` the router heartbeat reads, or None when this host does not feed the router.
///
/// `routerctl` keeps `offsets.json` in the inbox directory. It starts a file it has no offset
/// for, or whose saved offset is beyond the file size, at the end and so skips whatever was
/// filed before. The file is therefore used only while `offsets.json` holds a usable offset
/// (a number up to the current size) for it; with no entry it is created empty so the next
/// heartbeat records its offset. In both other cases the message is sent as usual.
fn router_inbox(dir: &Path) -> Option<PathBuf> {
    let offsets: Value = serde_json::from_slice(&fs::read(dir.join("offsets.json")).ok()?).ok()?;
    let offsets = offsets.as_object()?;
    let inbox = dir.join("L0.md");
    // routerctl looks up the full path first, then any key with the same file name.
    let saved: Vec<&Value> = match offsets.get(inbox.to_str()?) {
        Some(offset) => vec![offset],
        None => offsets
            .iter()
            .filter(|(key, _)| {
                Path::new(key)
                    .file_name()
                    .is_some_and(|name| name == "L0.md")
            })
            .map(|(_, offset)| offset)
            .collect(),
    };
    if saved.is_empty() {
        let _ = OpenOptions::new().create(true).append(true).open(&inbox);
        return None;
    }
    let size = fs::metadata(&inbox).ok()?.len();
    saved
        .iter()
        .all(|offset| offset.as_u64().is_some_and(|offset| offset <= size))
        .then_some(inbox)
}

/// Append one line to the inbox; the line is the delivery. The diversion row is accounting and
/// its failure leaves the message filed, so the send is still refused.
fn divert(
    inbox: &Path,
    sender: &str,
    to: &str,
    word: &str,
    message: &str,
    now: DateTime<Utc>,
) -> std::io::Result<std::io::Result<()>> {
    let stamp = now.to_rfc3339_opts(SecondsFormat::Secs, true);
    let line = format!("- {stamp} {sender}: {}\n", one_line(message));
    OpenOptions::new()
        .append(true)
        .open(inbox)?
        .write_all(line.as_bytes())?;
    // One JSON row per diversion: the count per day and sender is read from this file.
    let row = json!({"ts": stamp, "day": &stamp[..10], "sender": sender, "to": to, "word": word});
    let log = inbox.with_file_name("diversions.jsonl");
    Ok(OpenOptions::new()
        .create(true)
        .append(true)
        .open(log)
        .and_then(|mut file| writeln!(file, "{row}")))
}

pub fn hook(input: impl BufRead, diagnostic: &mut impl Write) -> i32 {
    match (state_dir(), inbox_dir()) {
        (Some(state), Some(inbox)) => hook_in(input, diagnostic, &state, &inbox, Utc::now()),
        _ => 0,
    }
}

pub fn hook_in(
    input: impl BufRead,
    diagnostic: &mut impl Write,
    state: &Path,
    inbox_dir: &Path,
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
    // One clock reading decides the time check and the filing: a refused message writes nothing,
    // and a message that passes is filed with the same `now`. Registered alone on SendMessage,
    // this hook cannot disagree with a second hook that read the clock a moment later.
    if let Some((token, reason)) = message_time::refusal(message, now) {
        let _ = writeln!(diagnostic, "message-time: {token} {reason}");
        return 2;
    }
    if role_of(to) != ROUTING_ROLE {
        return 0;
    }
    let Some(word) = routine_word(message) else {
        return 0;
    };
    let Some(inbox) = router_inbox(inbox_dir) else {
        return 0;
    };
    let sender = sender_name(state, &payload);
    let Ok(accounting) = divert(&inbox, &sender, to, word, message, now) else {
        return 0;
    };
    if let Err(error) = accounting {
        let _ = writeln!(
            diagnostic,
            "message-route: the line is filed but the diversion log failed: {error}"
        );
    }
    let _ = writeln!(
        diagnostic,
        "message-route: {word} line to {ROUTING_ROLE} was written to {} and not sent; \
         routing reads it at its heartbeat, do not resend. BLOCKED, GO-REQ, FAIL and \
         INCIDENT messages are delivered.",
        inbox.display()
    );
    2
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn roles_lose_a_counter_of_four_or_more_digits() {
        assert_eq!(role_of("routing-0789"), "routing");
        assert_eq!(role_of("plan-order-0745"), "plan-order");
        assert_eq!(role_of("routing"), "routing");
        assert_eq!(role_of("plan-order"), "plan-order");
        assert_eq!(role_of("writer-12"), "writer-12");
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

    /// An inbox directory the router tracks: `L0.md` empty with a saved offset of 0.
    fn tracked_inbox() -> tempfile::TempDir {
        let dir = tempfile::tempdir().unwrap();
        let file = dir.path().join("L0.md");
        fs::write(&file, "").unwrap();
        let offsets = json!({ file.to_str().unwrap(): 0 });
        fs::write(dir.path().join("offsets.json"), offsets.to_string()).unwrap();
        dir
    }

    /// Run the hook once at `now`: the exit code, its diagnostic, and the inbox text.
    fn send(inbox: &Path, now: &str, to: &str, message: &str) -> (i32, String, String) {
        let payload = json!({
            "hook_event_name": "PreToolUse", "session_id": "s", "tool_name": "SendMessage",
            "tool_input": {"to": to, "message": message},
        });
        let mut diagnostic = Vec::new();
        let state = tempfile::tempdir().unwrap();
        let code = hook_in(
            payload.to_string().as_bytes(),
            &mut diagnostic,
            state.path(),
            inbox,
            now.parse().unwrap(),
        );
        let filed = fs::read_to_string(inbox.join("L0.md")).unwrap();
        (code, String::from_utf8(diagnostic).unwrap(), filed)
    }

    #[test]
    fn the_two_minute_boundary_is_one_decision_before_any_write() {
        let inbox = tracked_inbox();
        let dir = inbox.path();
        // A stamp exactly 120 s old, then 119.5 s old: filed, once each.
        for now in ["2026-11-22T04:06:00Z", "2026-11-22T04:05:59.500Z"] {
            let (code, diagnostic, _) = send(dir, now, "routing", "DONE at 04:04:00Z");
            assert_eq!(code, 2, "{now}");
            assert!(diagnostic.contains("written to"), "{now}: {diagnostic}");
        }
        let filed = fs::read_to_string(dir.join("L0.md")).unwrap();
        assert_eq!(filed.lines().count(), 2, "{filed}");
        let rows = fs::read_to_string(dir.join("diversions.jsonl")).unwrap();
        assert_eq!(rows.lines().count(), 2, "{rows}");
        // 120.001 s old: refused by this hook, nothing filed or counted.
        let (code, diagnostic, after) = send(
            dir,
            "2026-11-22T04:06:00.001Z",
            "routing",
            "DONE at 04:04:00Z",
        );
        assert_eq!(code, 2);
        assert!(
            diagnostic.starts_with("message-time: 04:04:00Z"),
            "{diagnostic}"
        );
        assert_eq!(after, filed);
        let rows_after = fs::read_to_string(dir.join("diversions.jsonl")).unwrap();
        assert_eq!(rows_after, rows);
    }

    #[test]
    fn the_time_check_covers_every_recipient_and_a_current_stamp_passes() {
        let inbox = tracked_inbox();
        let now = "2026-11-24T05:27:25Z";
        let (code, diagnostic, _) = send(inbox.path(), now, "peer", "Done at 05:4xZ");
        assert_eq!(code, 2, "{diagnostic}");
        assert!(diagnostic.contains("placeholder"), "{diagnostic}");
        let (code, diagnostic, filed) = send(inbox.path(), now, "peer", "Done at 05:27Z");
        assert_eq!((code, diagnostic.as_str(), filed.as_str()), (0, "", ""));
        let (code, _, filed) = send(inbox.path(), now, "routing", "BLOCKED at 05:27Z");
        assert_eq!((code, filed.as_str()), (0, ""));
    }
}
