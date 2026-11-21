#![cfg(unix)]

use pg_worker_core::host_link::observe;
use serde_json::Value;
use std::{fs, path::Path, process::Command};

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

    let repo = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let result = Command::new("python3")
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
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0]["to"], "routing");
    assert_eq!(rows[0]["from"], "system");
    assert_eq!(rows[0]["key"], payload["item_key"]);
    assert_eq!(rows[0]["msg_id"], payload["event_id"]);
    assert!(rows[0]["body"]["text"]
        .as_str()
        .unwrap()
        .contains("build-a"));
}
