//! Hermetic `pg-spawnd serve` integration tests (Unix sockets, no network).

#![cfg(unix)]
#[cfg(unix)]
mod common;

use common::{assert_mode_eventually, wait_connect, ChildGuard, TempDir};
use std::ffi::OsStr;
use std::io::{BufRead, BufReader, Write};
use std::path::Path;
use std::time::Duration;

const FIXTURE: &str = include_str!("../src/tools_list.json");

fn bin() -> &'static str {
    env!("CARGO_BIN_EXE_pg-spawnd")
}

fn spawn_serve(socket: &Path) -> ChildGuard {
    ChildGuard::spawn(
        bin(),
        &[
            OsStr::new("serve"),
            OsStr::new("--socket"),
            socket.as_os_str(),
        ],
    )
}

struct McpClient {
    stream: BufReader<std::os::unix::net::UnixStream>,
    next_id: i64,
}

impl McpClient {
    fn connect(socket: &Path) -> McpClient {
        let stream = wait_connect(socket, Duration::from_secs(5));
        stream
            .set_read_timeout(Some(Duration::from_secs(10)))
            .expect("read timeout");
        McpClient {
            stream: BufReader::new(stream),
            next_id: 0,
        }
    }

    fn send(&mut self, message: &serde_json::Value) {
        let mut line = serde_json::to_string(message).expect("serialize");
        line.push('\n');
        self.stream
            .get_mut()
            .write_all(line.as_bytes())
            .expect("write request");
    }

    fn recv(&mut self) -> serde_json::Value {
        let mut line = String::new();
        self.stream.read_line(&mut line).expect("read response");
        assert!(!line.is_empty(), "server closed the connection");
        serde_json::from_str(line.trim_end_matches('\n')).expect("parse response")
    }

    fn request(&mut self, method: &str, params: serde_json::Value) -> serde_json::Value {
        self.next_id += 1;
        let id = self.next_id;
        self.send(&serde_json::json!({
            "jsonrpc": "2.0", "id": id, "method": method, "params": params,
        }));
        loop {
            let response = self.recv();
            if response.get("id") == Some(&serde_json::json!(id)) {
                return response;
            }
        }
    }

    fn initialize(&mut self) -> serde_json::Value {
        let response = self.request(
            "initialize",
            serde_json::json!({
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test-suite", "version": "0"},
            }),
        );
        self.send(&serde_json::json!({
            "jsonrpc": "2.0", "method": "notifications/initialized",
        }));
        response
    }
}

#[test]
fn socket_dir_is_0700_and_socket_is_0600() {
    let tmp = TempDir::new();
    let socket = tmp.path().join("sub").join("spawnd.sock");
    let _guard = spawn_serve(&socket);
    let _client = McpClient::connect(&socket);
    assert_mode_eventually(socket.parent().expect("parent"), 0o700);
    assert_mode_eventually(&socket, 0o600);
}

#[test]
fn initialize_reports_server_info() {
    let tmp = TempDir::new();
    let socket = tmp.path().join("spawnd.sock");
    let _guard = spawn_serve(&socket);
    let mut client = McpClient::connect(&socket);
    let response = client.initialize();
    let info = &response["result"]["serverInfo"];
    assert_eq!(info["name"], "pg-spawnd");
    assert_eq!(info["version"], env!("CARGO_PKG_VERSION"));
    assert!(response["result"]["protocolVersion"].is_string());
}

#[test]
fn tools_list_matches_embedded_fixture() {
    let tmp = TempDir::new();
    let socket = tmp.path().join("spawnd.sock");
    let _guard = spawn_serve(&socket);
    let mut client = McpClient::connect(&socket);
    client.initialize();
    let response = client.request("tools/list", serde_json::json!({}));
    let fixture: serde_json::Value = serde_json::from_str(FIXTURE).expect("fixture");
    assert_eq!(response["result"]["tools"], fixture);
    assert_eq!(
        response["result"]["tools"].as_array().expect("array").len(),
        15
    );
}

#[test]
fn health_tool_returns_expected_payload() {
    let tmp = TempDir::new();
    let socket = tmp.path().join("spawnd.sock");
    let _guard = spawn_serve(&socket);
    let mut client = McpClient::connect(&socket);
    client.initialize();
    let response = client.request(
        "tools/call",
        serde_json::json!({"name": "plangraph_agent_health", "arguments": {}}),
    );
    let result = &response["result"];
    assert_eq!(result["isError"], false);
    let text = result["content"][0]["text"].as_str().expect("text");
    let payload: serde_json::Value = serde_json::from_str(text).expect("payload");
    assert_eq!(
        payload,
        serde_json::json!({
            "status": "healthy",
            "daemon": "pg-spawnd",
            "spawner_version": format!("pg-spawnd {}", env!("CARGO_PKG_VERSION")),
            "placement": "off",
            "runner_jobs_total": 0,
            "active_jobs_count": 0,
            "catalog_models_count": 0,
            "gateway_default_url": "",
        })
    );
}

#[test]
fn not_implemented_tool_is_error_naming_r6a_3() {
    let tmp = TempDir::new();
    let socket = tmp.path().join("spawnd.sock");
    let _guard = spawn_serve(&socket);
    let mut client = McpClient::connect(&socket);
    client.initialize();
    let response = client.request(
        "tools/call",
        serde_json::json!({"name": "plangraph_session_list", "arguments": {}}),
    );
    assert_eq!(response["result"]["isError"], true);
    let text = response["result"]["content"][0]["text"]
        .as_str()
        .expect("text");
    assert!(
        text.contains("not implemented in pg-spawnd yet (R6a-3)"),
        "{text}"
    );
}

#[test]
fn unknown_tool_is_protocol_error_naming_no_internals() {
    let tmp = TempDir::new();
    let socket = tmp.path().join("spawnd.sock");
    let _guard = spawn_serve(&socket);
    let mut client = McpClient::connect(&socket);
    client.initialize();
    let response = client.request(
        "tools/call",
        serde_json::json!({"name": "plangraph_agent_exfiltrate", "arguments": {}}),
    );
    let error = &response["error"];
    assert!(error.is_object(), "{response}");
    let message = error["message"].as_str().expect("message");
    assert!(message.contains("unknown tool"), "{message}");
    assert!(
        !message.contains('/') && !message.contains('\\'),
        "{message}"
    );
}

#[test]
fn stale_socket_is_replaced() {
    let tmp = TempDir::new();
    let socket = tmp.path().join("spawnd.sock");
    {
        let _stale = std::os::unix::net::UnixListener::bind(&socket).expect("bind stale");
    }
    assert!(socket.exists(), "stale socket file left behind");
    let _guard = spawn_serve(&socket);
    let mut client = McpClient::connect(&socket);
    let response = client.initialize();
    assert_eq!(response["result"]["serverInfo"]["name"], "pg-spawnd");
}

#[test]
fn second_serve_on_live_socket_exits_two() {
    let tmp = TempDir::new();
    let socket = tmp.path().join("spawnd.sock");
    let _guard = spawn_serve(&socket);
    let _client = McpClient::connect(&socket);
    let output = std::process::Command::new(bin())
        .args([
            OsStr::new("serve"),
            OsStr::new("--socket"),
            socket.as_os_str(),
        ])
        .output()
        .expect("second serve");
    assert_eq!(output.status.code(), Some(2));
    let stderr = String::from_utf8(output.stderr).expect("utf-8");
    assert_eq!(stderr.lines().count(), 1, "{stderr}");
}

#[test]
fn serve_without_socket_or_env_exits_two() {
    let output = std::process::Command::new(bin())
        .args([OsStr::new("serve")])
        .env_remove("XDG_RUNTIME_DIR")
        .output()
        .expect("serve without socket");
    assert_eq!(output.status.code(), Some(2));
    let stderr = String::from_utf8(output.stderr).expect("utf-8");
    assert_eq!(stderr.lines().count(), 1, "{stderr}");
}

#[test]
fn serve_with_env_default_socket_path() {
    let tmp = TempDir::new();
    let runtime = tmp.path().join("runtime");
    let child = std::process::Command::new(bin())
        .args([OsStr::new("serve")])
        .env("XDG_RUNTIME_DIR", &runtime)
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::piped())
        .spawn()
        .expect("serve with env");
    let _guard = ChildGuard { child };
    let socket = runtime.join("plangraph").join("spawnd.sock");
    let mut client = McpClient::connect(&socket);
    let response = client.initialize();
    assert_eq!(response["result"]["serverInfo"]["name"], "pg-spawnd");
}
