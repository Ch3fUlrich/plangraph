#![cfg(unix)]

#[cfg(test)]
#[path = "../../test_support/python.rs"]
mod test_python;

use pg_worker_core::host_link::{observe, observe_ssh};
use serde_json::{json, Value};
use std::{
    fs,
    io::Write,
    process::{Command, Stdio},
};

#[test]
fn blocked_peer_alert_reaches_keeper_through_the_watchdog_bridge() {
    let fixture = tempfile::tempdir().unwrap();
    let state = fixture.path();
    observe(state, "build-a", "coordinator", 100, false).unwrap();
    observe(state, "build-a", "coordinator", 401, false).unwrap();
    let outbox = state.join("watchdog/outbox");
    let event = fs::read_dir(&outbox)
        .unwrap()
        .next()
        .unwrap()
        .unwrap()
        .path();
    let payload: Value = serde_json::from_slice(&fs::read(&event).unwrap()).unwrap();

    for (now, reachable) in [(500, false), (560, false), (620, true)] {
        if observe_ssh(state, "worker-a", now, reachable, || {
            json!({
                "host": "worker-a", "peer": "peer-a", "peer_state": {"Online": false},
                "last_ip_change": "fixture IP change"
            })
        })
        .unwrap()
        .is_some()
        {
            let (rule, name) = if reachable {
                ("host-ssh-recovered", "ssh-up.json")
            } else {
                ("host-ssh-down", "ssh-down.json")
            };
            let path = fs::read_dir(&outbox)
                .unwrap()
                .filter_map(Result::ok)
                .map(|entry| entry.path())
                .find(|path| path.file_name().unwrap().to_string_lossy().contains(rule))
                .unwrap();
            fs::rename(path, state.join(name)).unwrap();
        }
    }

    let rows = deliver_with_watchdog(state);
    assert_eq!(rows.len(), 3);
    assert_eq!(rows[0]["to"], "routing");
    assert_eq!(rows[0]["from"], "system");
    assert_eq!(rows[0]["key"], payload["item_key"]);
    assert_eq!(rows[0]["msg_id"], payload["event_id"]);
    assert!(rows[0]["body"]["text"]
        .as_str()
        .unwrap()
        .contains("build-a"));
    assert_eq!(rows[1]["body"]["text"], "worker-a unreachable since 500, peer peer-a state {\"Online\":false}, last local IP change \"fixture IP change\"");
    assert_eq!(
        rows[2]["body"]["text"],
        "worker-a recovered from the SSH outage since 500"
    );
}

fn deliver_with_watchdog(state: &std::path::Path) -> Vec<Value> {
    let result = test_python::command()
        .args([
            "-c",
            r#"
import json, os, sys, threading
from pathlib import Path
from plangraph.keeper import api, auth, config, db
from plangraph.keeper.store import Store
from plangraph.spawner.stall_watchdog import flush_outbox

state = Path(sys.argv[1])
outbox = state / "watchdog/outbox"
path = state / "keeper.db"
connection = db.open_db(path, create=True, backend="sqlite")
system_token = auth.add_principal(connection, "system", "system", [])
store = Store(connection)
conf = state / "keeper.json"
conf.write_text(json.dumps({"db": str(path), "bind": "127.0.0.1:0"}))
conf.chmod(0o600)
token = state / "token"
token.write_text(system_token)
server = api.make_server(store, config.load_config(conf), port=0)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
try:
    os.environ.update(PG_URL=f"http://127.0.0.1:{server.server_address[1]}",
                      PG_TOKEN_FILE=str(token), PG_HOST_LINK_SENDER="reader")
    bridge = [sys.argv[2], "host-link-deliver"]
    os.environ["PLANGRAPH_WATCHDOG_FALLBACK"] = json.dumps(["/bin/false"])
    pending = {event: event.read_bytes() for event in outbox.glob("*.json")}
    assert flush_outbox(state, bridge)["failures"] == len(pending)
    assert all(event.read_bytes() == data for event, data in pending.items())
    os.environ["PG_HOST_LINK_SENDER"] = "system"
    assert flush_outbox(state, bridge)["failures"] == 0
    assert not list(outbox.glob("*.json"))
    for event, data in pending.items():
        event.write_bytes(data)
    assert flush_outbox(state, bridge)["failures"] == 0
    for name in ("ssh-down.json", "ssh-up.json"):
        path = state / name
        if path.exists():
            path.rename(outbox / name)
    assert flush_outbox(state, bridge)["failures"] == 0
    assert not list(outbox.glob("*.json"))
    rows = store.messages(auth.authenticate(connection, system_token))
    print(json.dumps(rows))
finally:
    server.shutdown()
    server.server_close()
    thread.join(5)
    connection.close()
"#,
        ])
        .arg(state)
        .arg(env!("CARGO_BIN_EXE_plangraph"))
        .output()
        .unwrap();
    assert!(result.status.success(), "{result:?}");
    serde_json::from_slice(&result.stdout).unwrap()
}

#[test]
fn disk_hygiene_zero_progress_reaches_keeper() {
    let state = tempfile::tempdir().unwrap();
    let outbox = state.path().join("watchdog/outbox");
    fs::create_dir_all(&outbox).unwrap();
    let item = "plangraph:LE-DISK-HYGIENE-DEFERS-ALL-CLEANUP-WHILE-ANY-BUILD-RUNS";
    let kept = json!([
        {"reason": "busy", "path": "/fixture/line\n\t\"'\\é"},
        {"reason": "young", "path": format!("/fixture/{}/cache-tail", "é/".repeat(200))},
        {"reason": "unsafe", "path": "/fixture/source"},
        {"reason": "leased", "path": "/fixture/lease"},
        {"reason": "run_budget", "path": "/fixture/budget"}
    ]);
    let cases = [
        ("disk-hygiene-zero-progress", "a"),
        ("disk-hygiene-below-alarm-floor", "b"),
    ];
    for (freed, (rule, byte)) in cases.iter().enumerate() {
        let id = byte.repeat(64);
        let file = outbox.join(format!("{:020}-{rule}-{id}.json", 100));
        let event = json!({"kind": "alert", "rule": rule, "item_key": item,
            "recipient": "routing", "host": "fixture-worker", "event_id": id,
            "down_since": 100, "observed_at": 101, "device": 12,
            "end_free_bytes": 8, "floor_bytes": 20, "freed_bytes": freed, "kept": kept});
        fs::write(file, serde_json::to_vec(&event).unwrap()).unwrap();
    }
    let rows = deliver_with_watchdog(state.path());
    assert_eq!(rows.len(), 2);
    for (rule, byte) in cases {
        let id = byte.repeat(64);
        let row = rows.iter().find(|row| row["msg_id"] == id).unwrap();
        assert_eq!(row["to"], "routing");
        assert_eq!(row["key"], item);
        assert!(serde_json::to_vec(&row["body"]).unwrap().len() < 4096);
        let lines: Vec<_> = row["body"]["text"].as_str().unwrap().lines().collect();
        assert_eq!(lines.len(), 6);
        assert!(lines[0].contains(rule));
        assert_eq!(lines[1], "busy /fixture/line%0A%09%22%27%5C%C3%A9");
        let (prefix, suffix) = lines[2].split_once("...").unwrap();
        assert!(prefix.starts_with("young /fixture/") && suffix.ends_with("/cache-tail"));
        assert!(lines[2].len() <= "young ".len() + 384);
        assert_eq!(lines[3], "unsafe /fixture/source");
        assert_eq!(lines[4], "leased /fixture/lease");
        assert_eq!(lines[5], "run_budget /fixture/budget");
    }
}

#[test]
fn unrelated_watchdog_event_is_forwarded_unchanged_and_propagates_failure() {
    let event = b"{\"kind\": \"completion\", \"extra\": [1, 2]}\n";
    for (fallback, success) in [("/bin/cat", true), ("/bin/false", false)] {
        let mut child = Command::new(env!("CARGO_BIN_EXE_plangraph"))
            .arg("host-link-deliver")
            .env("PLANGRAPH_WATCHDOG_FALLBACK", json!([fallback]).to_string())
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        child.stdin.take().unwrap().write_all(event).unwrap();
        let result = child.wait_with_output().unwrap();
        assert_eq!(result.status.success(), success, "{result:?}");
        if success {
            assert_eq!(result.stdout, event);
        } else {
            assert!(String::from_utf8_lossy(&result.stderr).contains("watchdog fallback failed"));
        }
    }
}

#[test]
fn worker_outage_and_clear_reach_keeper_alerts_with_run_ids() {
    let state = tempfile::tempdir().unwrap();
    let result = test_python::command()
        .args([
            "-c",
            r#"
import json, os, subprocess, sys, threading
from pathlib import Path
from plangraph.keeper import api, auth, config, db
from plangraph.keeper.store import Store

root = Path(sys.argv[1])
connection = db.open_db(root / "keeper.db", create=True, backend="sqlite")
system_token = auth.add_principal(connection, "system", "system", [])
store = Store(connection)
conf = root / "keeper.json"
conf.write_text(json.dumps({"db": str(root / "keeper.db"), "bind": "127.0.0.1:0"}))
conf.chmod(0o600)
token = root / "token"
token.write_text(system_token)
server = api.make_server(store, config.load_config(conf), port=0)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
principal = auth.authenticate(connection, system_token)
event = root / "event.json"
env = dict(os.environ, PG_URL=f"http://127.0.0.1:{server.server_address[1]}",
           PG_TOKEN_FILE=str(token), PG_HOST_LINK_SENDER="system")
def send(status, mid, observed, until=None, recipient="routing"):
    event.write_text(json.dumps({
        "kind": "alert", "rule": "worker-host-" + status, "item_key": "plangraph:LESSON",
        "recipient": recipient, "host": "fixture-worker", "event_id": mid * 64,
        "down_since": 60, "observed_at": observed,
        "maintenance_until": until, "unknown_run_ids": ["run-example"],
    }))
    subprocess.run([sys.argv[2], "host-link-deliver", str(event)], env=env, check=True)
def findings():
    return store.alerts(principal, project="plangraph")["findings"]
try:
    send("maintenance", "a", 120, 180, "host-health")
    assert not findings()
    send("unreachable", "b", 180, 180)
    send("unreachable", "b", 180, 180)
    assert len(findings()) == 1
    alert = json.loads(findings()[0]["msg"])
    assert alert["unknown_run_ids"] == ["run-example"]
    assert alert["maintenance_until"] == 180
    send("recovered", "c", 240, 180)
    send("unreachable", "d", 180, 180)
    assert not findings()
    messages = store.messages(principal)
    clear = next(row for row in messages if row["msg_id"] == "c" * 64)
    assert clear["body"]["worker_host"]["unknown_run_ids"] == ["run-example"]
    rows = connection.execute("SELECT action FROM alert_log ORDER BY id").fetchall()
    assert [row["action"] for row in rows] == ["alert_raised", "alert_cleared"]
finally:
    server.shutdown()
    server.server_close()
    thread.join(5)
    connection.close()
"#,
        ])
        .arg(state.path())
        .arg(env!("CARGO_BIN_EXE_plangraph"))
        .output()
        .unwrap();
    assert!(result.status.success(), "{result:?}");
}
