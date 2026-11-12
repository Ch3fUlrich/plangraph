use crate::common::{ChildGuard, McpClient, TempDir};
use serde_json::{json, Value};
use std::os::unix::fs::PermissionsExt;
pub struct Fixture {
    pub state: TempDir,
    _guard: ChildGuard,
    client: McpClient,
}
impl Fixture {
    pub fn new(command: &str, stdin: Value, extra: &[&str]) -> Self {
        Self::configured(command, stdin, extra, false)
    }
    #[allow(dead_code)]
    pub fn missing_launch() -> Self {
        Self::configured("", Value::Null, &[], true)
    }
    fn configured(command: &str, stdin: Value, extra: &[&str], missing: bool) -> Self {
        let state = TempDir::new();
        let route = state.path().join("route");
        let launch = state.path().join("launch");
        let plan =
            json!({"legs":[],"first_leg":{"id":"leg","harness":"h","model":"m","family":"f"}});
        let secret = ["private", "-", "credential"].concat();
        let stdin = stdin.as_str().unwrap_or("fixture prompt");
        let spec = json!({"argv":["/bin/sh","-c",command],"cwd":state.path(),"env":{"API_TOKEN":secret},"prompt":stdin,"stdin":stdin,"warnings":["warning"],"events":[{"event":"adapter"}]});
        for (path, body) in [
            (&route, format!("printf '%s' '{plan}'")),
            (
                &launch,
                format!(
                    "cat > '{}/request'\nprintf '%s' '{spec}'",
                    state.path().display()
                ),
            ),
        ] {
            std::fs::write(path, format!("#!/bin/sh\n{body}\n")).unwrap();
            std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o755)).unwrap();
        }
        let socket = state.path().join("socket");
        let mut cmd = std::process::Command::new(env!("CARGO_BIN_EXE_pg-spawnd"));
        cmd.args(["serve", "--no-scope", "--socket"])
            .arg(&socket)
            .arg("--state")
            .arg(state.path())
            .args(extra)
            // Keep dispatch non-blocking probes valid even on a multi-thread runtime.
            .env("TOKIO_WORKER_THREADS", "1")
            .env("PG_SPAWND_ADAPTER", route)
            .env("PG_SPAWND_LAUNCH_CMD", launch);
        if missing {
            cmd.env_remove("PG_SPAWND_LAUNCH_CMD")
                .env("PG_SPAWND_PYTHON", state.path().join("missing"));
        }
        let child = cmd.spawn().unwrap();
        let guard = ChildGuard { child };
        let mut client = McpClient::connect(&socket);
        client.initialize();
        Self {
            state,
            _guard: guard,
            client,
        }
    }
    pub fn call(&mut self, verb: &str, args: Value) -> Value {
        self.client.request(
            "tools/call",
            json!({"name":format!("plangraph_agent_{verb}"),"arguments":args}),
        )
    }
    pub fn spawn(&mut self, wait: f64) -> Value {
        self.call(
            "spawn",
            json!({"prompt":"verbatim\nprompt","card":{"x":1},"wait_seconds":wait}),
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
