//! Hermetic `pg-spawnd shim` integration tests (Unix sockets, no network).

#![cfg(unix)]
#[cfg(unix)]
mod common;

use common::{ChildGuard, TempDir};
use std::ffi::OsStr;
use std::io::{Read, Write};
use std::path::Path;
use std::time::Duration;

fn bin() -> &'static str {
    env!("CARGO_BIN_EXE_pg-spawnd")
}

fn spawn_shim(socket: &Path) -> ChildGuard {
    ChildGuard::spawn_piped_io(
        bin(),
        &[
            OsStr::new("shim"),
            OsStr::new("--socket"),
            socket.as_os_str(),
        ],
    )
}

#[test]
fn shim_round_trips_bytes_both_ways() {
    let tmp = TempDir::new();
    let socket = tmp.path().join("spawnd.sock");
    let listener = std::os::unix::net::UnixListener::bind(&socket).expect("bind fake server");

    let from_server: Vec<u8> = (0..=255u8).cycle().take(4096).collect();
    let from_client: Vec<u8> = b"client payload \x00\x01\x02\r\n".repeat(1024);
    let server_payload = from_server.clone();
    let expected_client = from_client.clone();
    let server = std::thread::spawn(move || {
        let (mut stream, _) = listener.accept().expect("accept");
        stream.write_all(&server_payload).expect("server write");
        let mut got = vec![0u8; expected_client.len()];
        stream.read_exact(&mut got).expect("server read");
        got
    });

    let mut guard = spawn_shim(&socket);
    let mut stdin = guard.child.stdin.take().expect("stdin");
    let mut stdout = guard.child.stdout.take().expect("stdout");
    stdin.write_all(&from_client).expect("client write");

    let mut echoed = vec![0u8; from_server.len()];
    stdout.read_exact(&mut echoed).expect("shim stdout");
    assert_eq!(echoed, from_server, "bytes from server survive the shim");

    let received = server.join().expect("server thread");
    assert_eq!(received, from_client, "bytes from client survive the shim");
    drop(stdin);

    for _ in 0..200 {
        if let Some(status) = guard.child.try_wait().expect("try_wait") {
            assert_eq!(status.code(), Some(0), "clean EOF exits 0");
            return;
        }
        std::thread::sleep(Duration::from_millis(25));
    }
    panic!("shim did not exit after the connection closed");
}

#[test]
fn shim_on_missing_socket_exits_two() {
    let tmp = TempDir::new();
    let socket = tmp.path().join("missing.sock");
    let output = std::process::Command::new(bin())
        .args([
            OsStr::new("shim"),
            OsStr::new("--socket"),
            socket.as_os_str(),
        ])
        .output()
        .expect("shim on missing socket");
    assert_eq!(output.status.code(), Some(2));
    let stderr = String::from_utf8(output.stderr).expect("utf-8");
    assert_eq!(stderr.lines().count(), 1, "{stderr}");
}

#[test]
fn shim_on_unreachable_socket_exits_two() {
    let tmp = TempDir::new();
    let socket = tmp.path().join("stale.sock");
    {
        let _stale = std::os::unix::net::UnixListener::bind(&socket).expect("bind stale");
    }
    let output = std::process::Command::new(bin())
        .args([
            OsStr::new("shim"),
            OsStr::new("--socket"),
            socket.as_os_str(),
        ])
        .output()
        .expect("shim on stale socket");
    assert_eq!(output.status.code(), Some(2));
    let stderr = String::from_utf8(output.stderr).expect("utf-8");
    assert_eq!(stderr.lines().count(), 1, "{stderr}");
}

#[test]
fn shim_without_socket_or_env_exits_two() {
    let output = std::process::Command::new(bin())
        .args([OsStr::new("shim")])
        .env_remove("XDG_RUNTIME_DIR")
        .output()
        .expect("shim without socket");
    assert_eq!(output.status.code(), Some(2));
    let stderr = String::from_utf8(output.stderr).expect("utf-8");
    assert_eq!(stderr.lines().count(), 1, "{stderr}");
}
