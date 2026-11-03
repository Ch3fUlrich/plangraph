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
    let (server_tx, server_rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let (mut stream, _) = listener.accept().expect("accept");
        stream
            .set_read_timeout(Some(Duration::from_secs(2)))
            .unwrap();
        stream
            .set_write_timeout(Some(Duration::from_secs(2)))
            .unwrap();
        stream.write_all(&server_payload).expect("server write");
        let mut got = vec![0u8; expected_client.len()];
        stream.read_exact(&mut got).expect("server read");
        server_tx.send(got).unwrap();
    });

    let mut guard = spawn_shim(&socket);
    let mut stdin = guard.child.stdin.take().expect("stdin");
    let mut stdout = guard.child.stdout.take().expect("stdout");
    stdin.write_all(&from_client).expect("client write");

    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let mut echoed = vec![0u8; from_server.len()];
        stdout.read_exact(&mut echoed).expect("shim stdout");
        tx.send(echoed).unwrap();
    });
    let echoed = rx
        .recv_timeout(Duration::from_secs(2))
        .expect("bounded stdout");
    assert_eq!(
        echoed,
        server_payload_for_assert(),
        "bytes from server survive the shim"
    );

    let received = server_rx
        .recv_timeout(Duration::from_secs(2))
        .expect("server thread");
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

fn server_payload_for_assert() -> Vec<u8> {
    (0..=255u8).cycle().take(4096).collect()
}

#[test]
fn stalled_stdout_after_eof_has_bounded_exit() {
    let tmp = TempDir::new();
    let socket = tmp.path().join("socket");
    let listener = std::os::unix::net::UnixListener::bind(&socket).unwrap();
    let mut guard = spawn_shim(&socket);
    listener.set_nonblocking(true).unwrap();
    let start = std::time::Instant::now();
    let mut stream = loop {
        if let Ok((stream, _)) = listener.accept() {
            break stream;
        }
        assert!(start.elapsed() < Duration::from_secs(2));
        std::thread::sleep(Duration::from_millis(10));
    };
    stream.set_nonblocking(true).unwrap();
    // Keep stdout's pipe open without reading; fill it and pin a blocking write.
    let _ = stream.write_all(&vec![b'x'; 1024 * 1024]);
    drop(guard.child.stdin.take());
    let eof = std::time::Instant::now();
    loop {
        if let Some(status) = guard.child.try_wait().unwrap() {
            assert_eq!(status.code(), Some(0));
            assert!(eof.elapsed() >= Duration::from_millis(4800));
            break;
        }
        assert!(eof.elapsed() < Duration::from_millis(5500));
        std::thread::sleep(Duration::from_millis(10));
    }
}

#[cfg(target_os = "linux")]
#[test]
fn pending_connect_times_out_without_retry() {
    let tmp = TempDir::new();
    let socket = tmp.path().join("spawnd.sock");
    let started = std::time::Instant::now();
    let child = std::process::Command::new(bin())
        .args([
            OsStr::new("shim"),
            OsStr::new("--socket"),
            socket.as_os_str(),
        ])
        .env("PG_SPAWND_CONNECT_TIMEOUT_MS", u64::MAX.to_string())
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .spawn()
        .unwrap();
    let mut guard = ChildGuard { child };
    loop {
        if let Some(status) = guard.child.try_wait().unwrap() {
            assert_eq!(status.code(), Some(2));
            assert!(started.elapsed() >= Duration::from_millis(1800));
            break;
        }
        assert!(
            started.elapsed() < Duration::from_millis(2500),
            "connect deadline exceeded"
        );
        std::thread::sleep(Duration::from_millis(10));
    }
}

#[test]
fn stdin_eof_preserves_full_delayed_reply() {
    let tmp = TempDir::new();
    let socket = tmp.path().join("spawnd.sock");
    let listener = std::os::unix::net::UnixListener::bind(&socket).unwrap();
    let mut guard = spawn_shim(&socket);
    let mut stdin = guard.child.stdin.take().unwrap();
    stdin.write_all(b"request").unwrap();
    drop(stdin);
    let mut stdout = guard.child.stdout.take().unwrap();
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let mut reply = Vec::new();
        stdout.read_to_end(&mut reply).unwrap();
        tx.send(reply).unwrap();
    });
    listener.set_nonblocking(true).unwrap();
    let deadline = std::time::Instant::now() + Duration::from_secs(2);
    let mut stream = loop {
        if let Ok((stream, _)) = listener.accept() {
            break stream;
        }
        assert!(std::time::Instant::now() < deadline);
        std::thread::sleep(Duration::from_millis(10));
    };
    stream
        .set_read_timeout(Some(Duration::from_secs(2)))
        .unwrap();
    stream
        .set_write_timeout(Some(Duration::from_secs(2)))
        .unwrap();
    let mut request = Vec::new();
    stream.read_to_end(&mut request).unwrap();
    assert_eq!(request, b"request");
    std::thread::sleep(Duration::from_millis(50));
    let reply = vec![b'x'; 32768];
    stream.write_all(&reply).unwrap();
    drop(stream);
    assert_eq!(rx.recv_timeout(Duration::from_secs(2)).unwrap(), reply);
}

#[test]
fn shim_on_missing_socket_exits_two() {
    let tmp = TempDir::new();
    let socket = tmp.path().join("missing.sock");
    let output = common::output(std::process::Command::new(bin()).args([
        OsStr::new("shim"),
        OsStr::new("--socket"),
        socket.as_os_str(),
    ]));
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
    let output = common::output(std::process::Command::new(bin()).args([
        OsStr::new("shim"),
        OsStr::new("--socket"),
        socket.as_os_str(),
    ]));
    assert_eq!(output.status.code(), Some(2));
    let stderr = String::from_utf8(output.stderr).expect("utf-8");
    assert_eq!(stderr.lines().count(), 1, "{stderr}");
}

#[test]
fn shim_without_socket_or_env_exits_two() {
    let output = common::output(
        std::process::Command::new(bin())
            .args([OsStr::new("shim")])
            .env_remove("XDG_RUNTIME_DIR"),
    );
    assert_eq!(output.status.code(), Some(2));
    let stderr = String::from_utf8(output.stderr).expect("utf-8");
    assert_eq!(stderr.lines().count(), 1, "{stderr}");
}
