#![cfg(unix)]

#[cfg(test)]
#[path = "../../test_support/python.rs"]
mod test_python;

use pg_worker_core::host_link::{observe, observe_ssh};
use serde_json::{json, Value};
use std::{
    fs,
    io::Write,
    path::Path,
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

    let repo = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let result = test_python::command()
        .args([
            "-c",
            r#"
import json, os, sys, threading
from pathlib import Path
from plangraph.keeper import api, auth, config, db
from plangraph.keeper.store import Store
from plangraph.spawner.stall_watchdog import flush_outbox

state, event = map(Path, sys.argv[1:3])
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
    bridge = [sys.argv[3], "host-link-deliver"]
    assert flush_outbox(state, bridge)["failures"] == 1
    data = event.read_bytes()
    os.environ["PG_HOST_LINK_SENDER"] = "system"
    assert flush_outbox(state, bridge)["failures"] == 0
    assert not event.exists()
    event.write_bytes(data)
    assert flush_outbox(state, bridge)["failures"] == 0
    assert not event.exists()
    for name in ("ssh-down.json", "ssh-up.json"):
        path = state / name
        path.rename(event.parent / name)
    assert flush_outbox(state, bridge)["failures"] == 0
    assert not list(event.parent.glob("*.json"))
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
        .arg(&event)
        .arg(env!("CARGO_BIN_EXE_plangraph"))
        .env("PYTHONPATH", repo.join("src"))
        .output()
        .unwrap();
    assert!(result.status.success(), "{result:?}");
    let rows: Value = serde_json::from_slice(&result.stdout).unwrap();
    let rows = rows.as_array().unwrap();
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
    let repo = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let result = Command::new("python3")
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
        .env("PYTHONPATH", repo.join("src"))
        .output()
        .unwrap();
    assert!(result.status.success(), "{result:?}");
}
