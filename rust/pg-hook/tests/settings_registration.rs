#![cfg(unix)]
//! The SendMessage guards run through the hook commands the repository's Claude settings register.
use serde_json::{json, Value};
use std::io::Write;
use std::path::Path;
use std::process::{Command, Stdio};

fn registered_commands() -> Vec<String> {
    let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../.claude/settings.json");
    let settings: Value = serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap();
    settings["hooks"]["PreToolUse"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|group| group["matcher"] == "SendMessage")
        .flat_map(|group| group["hooks"].as_array().unwrap())
        .inspect(|hook| assert_eq!(hook["type"], "command"))
        .map(|hook| hook["command"].as_str().unwrap().to_owned())
        .collect()
}

/// Exit code of every registered command for one SendMessage payload, in registration order.
fn run_registered(home: &Path, state: &Path, to: &str, message: &str) -> Vec<Option<i32>> {
    let payload = json!({
        "hook_event_name": "PreToolUse",
        "session_id": "s",
        "tool_name": "SendMessage",
        "tool_input": {"to": to, "message": message},
    });
    registered_commands()
        .iter()
        .map(|command| {
            let mut child = Command::new("sh")
                .args(["-c", command])
                .env("HOME", home)
                .env("XDG_STATE_HOME", state)
                .env("ROUTING_STATE_DIR", state.join("routing"))
                .env_remove("ROUTER_INBOX_DIR")
                .stdin(Stdio::piped())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .spawn()
                .unwrap();
            // A missing binary exits before reading the payload.
            let _ = writeln!(child.stdin.take().unwrap(), "{payload}");
            child.wait().unwrap().code()
        })
        .collect()
}

fn install(home: &Path) {
    let hooks = home.join(".local/share/plangraph/hooks");
    std::fs::create_dir_all(&hooks).unwrap();
    std::os::unix::fs::symlink(env!("CARGO_BIN_EXE_pg-hook"), hooks.join("pg-hook")).unwrap();
}

#[test]
fn the_guard_is_registered_on_sendmessage_and_refuses_through_the_registration() {
    let home = tempfile::tempdir().unwrap();
    install(home.path());
    let state = tempfile::tempdir().unwrap();
    let inbox = state.path().join("routing/inbox");
    std::fs::create_dir_all(&inbox).unwrap();
    let key = inbox.join("L0.md");
    std::fs::write(&key, "").unwrap();
    std::fs::write(
        inbox.join("offsets.json"),
        json!({key.to_str().unwrap(): 0}).to_string(),
    )
    .unwrap();
    // One command decides the time check and the filing on a single clock reading.
    assert_eq!(registered_commands().len(), 1);
    assert!(registered_commands()[0].contains("message-route"));
    assert_eq!(
        run_registered(
            home.path(),
            state.path(),
            "peer",
            "Created at 2099-01-01T04:02:00Z"
        ),
        [Some(2)]
    );
    assert_eq!(
        run_registered(home.path(), state.path(), "routing", "DONE train 29"),
        [Some(2)]
    );
    assert_eq!(
        run_registered(home.path(), state.path(), "routing", "BLOCKED review"),
        [Some(0)]
    );
    // A routine line with a placeholder time is refused and never filed.
    let filed = std::fs::read_to_string(&key).unwrap();
    assert_eq!(filed.lines().count(), 1, "{filed}");
    assert_eq!(
        run_registered(home.path(), state.path(), "routing", "DONE at 05:4xZ"),
        [Some(2)]
    );
    assert_eq!(std::fs::read_to_string(&key).unwrap(), filed);
    let rows = std::fs::read_to_string(inbox.join("diversions.jsonl")).unwrap();
    assert_eq!(rows.lines().count(), 1, "{rows}");
}

#[test]
fn a_host_without_the_installed_binary_sends_the_message() {
    let home = tempfile::tempdir().unwrap();
    let state = tempfile::tempdir().unwrap();
    assert_eq!(
        run_registered(home.path(), state.path(), "routing", "DONE x"),
        [Some(1)]
    );
}
