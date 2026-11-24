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
                .env("ROUTING_STATE_DIR", state)
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
fn both_guards_are_registered_on_sendmessage_and_refuse_through_the_registration() {
    let home = tempfile::tempdir().unwrap();
    install(home.path());
    let state = tempfile::tempdir().unwrap();
    let inbox = state.path().join("inbox");
    std::fs::create_dir_all(&inbox).unwrap();
    let key = inbox.join("L0.md");
    std::fs::write(&key, "").unwrap();
    std::fs::write(
        inbox.join("offsets.json"),
        json!({key.to_str().unwrap(): 0}).to_string(),
    )
    .unwrap();
    assert_eq!(registered_commands().len(), 2);
    // message-time then message-route: a guessed stamp is refused by the first only.
    assert_eq!(
        run_registered(
            home.path(),
            state.path(),
            "peer",
            "Created at 2099-01-01T04:02:00Z"
        ),
        [Some(2), Some(0)]
    );
    assert_eq!(
        run_registered(home.path(), state.path(), "routing", "DONE train 29"),
        [Some(0), Some(2)]
    );
    assert_eq!(
        run_registered(home.path(), state.path(), "routing", "BLOCKED review"),
        [Some(0), Some(0)]
    );
}

#[test]
fn a_host_without_the_installed_binary_sends_the_message() {
    let home = tempfile::tempdir().unwrap();
    let state = tempfile::tempdir().unwrap();
    assert_eq!(
        run_registered(home.path(), state.path(), "routing", "DONE x"),
        [Some(1), Some(1)]
    );
}
