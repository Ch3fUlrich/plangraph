//! Hermetic `pg-spawnd serve` integration tests (Unix sockets, no network).

#![cfg(unix)]
#[cfg(unix)]
mod common;

use common::{assert_mode_eventually, ChildGuard, McpClient, TempDir};
use std::ffi::OsStr;
use std::io::Write;
use std::path::Path;
use std::time::Duration;

const FIXTURE: &str = include_str!("../src/tools_list.json");

#[test]
fn state_is_required_and_never_created() {
    let tmp = TempDir::new();
    let socket = tmp.path().join("socket");
    let output = common::output(
        std::process::Command::new(bin())
            .args([
                OsStr::new("serve"),
                OsStr::new("--socket"),
                socket.as_os_str(),
            ])
            .env_remove("HOME")
            .env_remove("USERPROFILE")
            .env_remove("XDG_STATE_HOME")
            .env_remove("PLANGRAPH_SPAWN_STATE"),
    );
    assert_eq!(output.status.code(), Some(2));
    assert_eq!(String::from_utf8(output.stderr).unwrap().lines().count(), 1);
    let state = tmp.path().join("absent");
    let _guard = ChildGuard::spawn(
        bin(),
        &[
            OsStr::new("serve"),
            OsStr::new("--socket"),
            socket.as_os_str(),
            OsStr::new("--state"),
            state.as_os_str(),
        ],
    );
    let mut client = McpClient::connect(&socket);
    client.initialize();
    let response = client.request(
        "tools/call",
        serde_json::json!({"name":"plangraph_agent_ps"}),
    );
    assert_eq!(
        response["result"]["content"][0]["text"],
        "{\n  \"runs\": []\n}"
    );
    assert!(!state.exists());
}

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

#[test]
fn concurrent_idle_and_disconnected_clients_do_not_block() {
    let tmp = TempDir::new();
    let socket = tmp.path().join("spawnd.sock");
    let _guard = spawn_serve(&socket);
    let idle = McpClient::connect(&socket);
    let mut first = McpClient::connect(&socket);
    assert!(first.initialize()["result"].is_object());
    let mut second = McpClient::connect(&socket);
    assert!(second.initialize()["result"].is_object());
    drop(idle);
    first.stream.get_mut().write_all(b"{\"jsonrpc\":").unwrap();
    drop(first);
    assert!(second.request("tools/list", serde_json::json!({}))["result"].is_object());
}

fn refused(socket: &Path) {
    let mut guard = spawn_serve(socket);
    let deadline = std::time::Instant::now() + Duration::from_secs(2);
    loop {
        if let Some(status) = guard.child.try_wait().unwrap() {
            assert_eq!(status.code(), Some(2));
            return;
        }
        assert!(
            std::time::Instant::now() < deadline,
            "serve did not refuse path"
        );
        std::thread::sleep(Duration::from_millis(10));
    }
}

#[test]
fn serve_refuses_group_readable_dir() {
    use std::os::unix::fs::PermissionsExt;
    let tmp = TempDir::new();
    for mode in [0o750, 0o705] {
        std::fs::set_permissions(tmp.path(), std::fs::Permissions::from_mode(mode)).unwrap();
        refused(&tmp.path().join("spawnd.sock"));
    }
}

#[test]
#[ignore = "requires root to change directory ownership"]
fn serve_refuses_dir_owned_by_other_user() {
    extern "C" {
        fn geteuid() -> u32;
        fn chown(path: *const std::ffi::c_char, owner: u32, group: u32) -> i32;
    }
    // Changing ownership requires root; unprivileged runs cannot exercise this.
    if unsafe { geteuid() } != 0 {
        return;
    }
    use std::os::unix::ffi::OsStrExt;
    let tmp = TempDir::new();
    let path = std::ffi::CString::new(tmp.path().as_os_str().as_bytes()).unwrap();
    assert_eq!(unsafe { chown(path.as_ptr(), 1, 1) }, 0);
    refused(&tmp.path().join("spawnd.sock"));
}

#[test]
fn serve_refuses_regular_file_at_socket_path() {
    let tmp = TempDir::new();
    let socket = tmp.path().join("spawnd.sock");
    std::fs::write(&socket, b"keep me").unwrap();
    refused(&socket);
    assert_eq!(std::fs::read(socket).unwrap(), b"keep me");
}

#[test]
fn serve_refuses_symlinked_dir_and_relative_path() {
    let tmp = TempDir::new();
    let link = tmp.path().join("link");
    std::os::unix::fs::symlink(tmp.path(), &link).unwrap();
    refused(&link.join("spawnd.sock"));
    let result = common::output(
        std::process::Command::new(bin())
            .args(["serve", "--socket", "spawnd.sock"])
            .current_dir(tmp.path()),
    );
    assert_eq!(result.status.code(), Some(2));
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
        text.contains("not implemented in pg-spawnd yet (R9)"),
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
    let output = common::output(std::process::Command::new(bin()).args([
        OsStr::new("serve"),
        OsStr::new("--socket"),
        socket.as_os_str(),
    ]));
    assert_eq!(output.status.code(), Some(2));
    let stderr = String::from_utf8(output.stderr).expect("utf-8");
    assert_eq!(stderr.lines().count(), 1, "{stderr}");
}

#[test]
fn serve_without_socket_or_env_exits_two() {
    let output = common::output(
        std::process::Command::new(bin())
            .args([OsStr::new("serve")])
            .env_remove("XDG_RUNTIME_DIR"),
    );
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

#[test]
fn idle_handshake_times_out_and_server_accepts_again() {
    use std::io::Read;
    let tmp = TempDir::new();
    let socket = tmp.path().join("spawnd.sock");
    let _guard = ChildGuard {
        child: std::process::Command::new(bin())
            .args([
                std::ffi::OsStr::new("serve"),
                std::ffi::OsStr::new("--socket"),
                socket.as_os_str(),
            ])
            .env("PG_SPAWND_CONNECT_TIMEOUT_MS", "100")
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::piped())
            .spawn()
            .unwrap(),
    };
    let mut idle = common::wait_connect(&socket, Duration::from_secs(2));
    idle.set_read_timeout(Some(Duration::from_secs(1))).unwrap();
    assert_eq!(idle.read(&mut [0]).unwrap(), 0);
    let mut next = McpClient::connect(&socket);
    assert!(next.initialize()["result"].is_object());
}

#[test]
fn parallel_stale_sockets_are_independent() {
    let barrier = std::sync::Barrier::new(2);
    std::thread::scope(|scope| {
        for _ in 0..2 {
            scope.spawn(|| {
                let tmp = TempDir::new();
                let socket = tmp.path().join("spawnd.sock");
                drop(std::os::unix::net::UnixListener::bind(&socket).unwrap());
                barrier.wait();
                let _guard = spawn_serve(&socket);
                let mut client = McpClient::connect(&socket);
                assert_eq!(
                    client.initialize()["result"]["serverInfo"]["name"],
                    "pg-spawnd"
                );
            });
        }
    });
}
