#![cfg(unix)]
#[path = "common/mod.rs"]
mod common;
use common::{ChildGuard, McpClient, TempDir};
use serde_json::{json, Value};
use std::os::unix::fs::PermissionsExt;
pub struct Fixture {
    pub(crate) _state: TempDir,
    _guard: ChildGuard,
    client: McpClient,
}
impl Fixture {
    pub fn new(item: Option<&str>) -> Self {
        let state = TempDir::new();
        for (name, value) in [
            (
                "route",
                json!({"legs":[],"first_leg":{"id":"leg","harness":"h","model":"m","family":"f"}}),
            ),
            (
                "launch",
                json!({"argv":["/bin/sh","-c","exit 0"],"cwd":state.path(),"env":{},"stdin":"p","prompt":"p","warnings":[],"events":[]}),
            ),
        ] {
            let path = state.path().join(name);
            std::fs::write(&path, format!("#!/bin/sh\nprintf '%s' '{value}'\n")).unwrap();
            std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o755)).unwrap();
        }
        let socket = state.path().join("socket");
        let child = std::process::Command::new(env!("CARGO_BIN_EXE_pg-spawnd"))
            .args(["serve", "--no-scope", "--socket"])
            .arg(&socket)
            .arg("--state")
            .arg(state.path())
            .env("PLANGRAPH_SPAWN_ITEM", item.unwrap_or(""))
            .env("PG_SPAWND_ADAPTER", state.path().join("route"))
            .env("PG_SPAWND_LAUNCH_CMD", state.path().join("launch"))
            .spawn()
            .unwrap();
        let mut client = McpClient::connect(&socket);
        client.initialize();
        Self {
            _state: state,
            _guard: ChildGuard { child },
            client,
        }
    }
    pub fn call(&mut self, verb: &str, args: Value) -> Value {
        self.client.request(
            "tools/call",
            json!({"name":format!("plangraph_agent_{verb}"),"arguments":args}),
        )
    }
}
pub fn payload(reply: Value) -> Value {
    serde_json::from_str(
        reply["result"]["content"][0]["text"]
            .as_str()
            .unwrap_or_else(|| panic!("{reply}")),
    )
    .unwrap()
}
