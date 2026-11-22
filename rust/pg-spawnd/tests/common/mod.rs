//! Shared helpers for pg-spawnd CLI integration tests.
#![allow(dead_code)] // each test binary uses a different subset

use std::os::unix::fs::{DirBuilderExt, PermissionsExt};
use std::path::Path;
use std::time::{Duration, Instant};

/// An exclusive, private scratch directory removed on drop.
pub(crate) struct TempDir(tempfile::TempDir);
impl TempDir {
    pub(crate) fn new() -> Self {
        let dir = tempfile::Builder::new()
            .prefix("pg-spawnd-it-")
            .permissions(std::fs::Permissions::from_mode(0o700))
            .tempdir()
            .unwrap();
        std::fs::set_permissions(dir.path(), std::fs::Permissions::from_mode(0o700)).unwrap();
        Self(dir)
    }
    pub(crate) fn path(&self) -> &Path {
        self.0.path()
    }
}

/// Kills and reaps the child process on drop.
pub(crate) struct ChildGuard {
    pub(crate) child: std::process::Child,
}

impl ChildGuard {
    pub(crate) fn spawn(bin: &str, args: &[&std::ffi::OsStr]) -> ChildGuard {
        let child = std::process::Command::new(bin)
            .args(args)
            .stdin(std::process::Stdio::null())
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::piped())
            .spawn()
            .expect("spawn child");
        ChildGuard { child }
    }

    /// Spawn with piped stdin/stdout (for shim tests).
    pub(crate) fn spawn_piped_io(bin: &str, args: &[&std::ffi::OsStr]) -> ChildGuard {
        let child = std::process::Command::new(bin)
            .args(args)
            .stdin(std::process::Stdio::piped())
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped())
            .spawn()
            .expect("spawn child");
        ChildGuard { child }
    }
}

impl Drop for ChildGuard {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let deadline = Instant::now() + Duration::from_secs(2);
        while self.child.try_wait().ok().flatten().is_none() && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(10));
        }
    }
}

pub(crate) fn output(command: &mut std::process::Command) -> std::process::Output {
    command
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped());
    let mut guard = ChildGuard {
        child: command.spawn().expect("spawn"),
    };
    let deadline = Instant::now() + Duration::from_secs(2);
    loop {
        if guard.child.try_wait().unwrap().is_some() {
            break;
        }
        assert!(Instant::now() < deadline, "child exit deadline");
        std::thread::sleep(Duration::from_millis(10));
    }
    use std::io::Read;
    let status = guard.child.try_wait().unwrap().unwrap();
    let mut stdout = Vec::new();
    let mut stderr = Vec::new();
    guard
        .child
        .stdout
        .take()
        .unwrap()
        .read_to_end(&mut stdout)
        .unwrap();
    guard
        .child
        .stderr
        .take()
        .unwrap()
        .read_to_end(&mut stderr)
        .unwrap();
    std::process::Output {
        status,
        stdout,
        stderr,
    }
}

/// Wait until `path` accepts a Unix connection, or panic after `timeout`.
pub(crate) fn wait_connect(socket: &Path, timeout: Duration) -> std::os::unix::net::UnixStream {
    let deadline = Instant::now() + timeout;
    loop {
        match std::os::unix::net::UnixStream::connect(socket) {
            Ok(stream) => return stream,
            Err(_) if Instant::now() < deadline => {
                std::thread::sleep(Duration::from_millis(25));
            }
            Err(e) => panic!("server never listened on {}: {e}", socket.display()),
        }
    }
}

/// Poll until the file mode equals `expected`, or panic after five seconds.
pub(crate) fn assert_mode_eventually(path: &Path, expected: u32) {
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        let mode = std::fs::metadata(path).expect("stat").permissions().mode() & 0o777;
        if mode == expected {
            return;
        }
        if Instant::now() >= deadline {
            assert_eq!(mode, expected, "mode of {}", path.display());
        }
        std::thread::sleep(Duration::from_millis(25));
    }
}

fn create_private_dir(path: &Path) -> std::io::Result<()> {
    std::fs::DirBuilder::new().mode(0o700).create(path)
}

/// Bind without listening so even a descriptor inherited by a concurrent spawn
/// cannot accept clients before exec closes it. Dropping this leaves a stale path.
pub(crate) fn bind_stale_socket(path: &Path) -> socket2::Socket {
    let socket = socket2::Socket::new(socket2::Domain::UNIX, socket2::Type::STREAM, None)
        .expect("create stale socket");
    socket
        .bind(&socket2::SockAddr::unix(path).expect("socket address"))
        .expect("bind stale socket");
    socket
}

#[test]
fn stale_socket_refuses_connections_with_inherited_descriptor() {
    let tmp = TempDir::new();
    let path = tmp.path().join("stale.sock");
    let socket = bind_stale_socket(&path);
    // Keep a duplicate alive, as a child between fork and exec would do.
    let inherited = socket.try_clone().unwrap();
    drop(socket);
    assert!(path.exists(), "stale socket file left behind");
    assert_eq!(
        std::os::unix::net::UnixStream::connect(&path)
            .unwrap_err()
            .kind(),
        std::io::ErrorKind::ConnectionRefused
    );
    drop(inherited);
}

#[test]
fn temp_dir_creation_is_exclusive_and_private() {
    let parent = TempDir::new();
    let path = parent.path().join("exclusive");
    create_private_dir(&path).unwrap();
    assert_eq!(
        std::fs::metadata(&path).unwrap().permissions().mode() & 0o777,
        0o700
    );
    assert_eq!(
        create_private_dir(&path).unwrap_err().kind(),
        std::io::ErrorKind::AlreadyExists
    );
}

use std::io::{BufRead, BufReader, ErrorKind, Write};
pub(crate) struct McpClient {
    pub(crate) stream: BufReader<std::os::unix::net::UnixStream>,
    next_id: i64,
    response_timeout: Duration,
}

impl McpClient {
    pub(crate) fn connect(socket: &Path) -> McpClient {
        let stream = wait_connect(socket, Duration::from_secs(5));
        McpClient {
            stream: BufReader::new(stream),
            next_id: 0,
            response_timeout: Duration::from_secs(15),
        }
    }

    pub(crate) fn set_response_timeout(&mut self, timeout: Duration) {
        self.response_timeout = timeout;
    }

    pub(crate) fn send(&mut self, message: &serde_json::Value) {
        let mut line = serde_json::to_string(message).expect("serialize");
        line.push('\n');
        self.stream
            .get_mut()
            .write_all(line.as_bytes())
            .expect("write request");
    }

    pub(crate) fn recv(&mut self) -> serde_json::Value {
        self.recv_until(Instant::now() + self.response_timeout)
    }

    fn recv_until(&mut self, deadline: Instant) -> serde_json::Value {
        let mut line = Vec::new();
        loop {
            let remaining = deadline.saturating_duration_since(Instant::now());
            assert!(!remaining.is_zero(), "MCP response deadline exceeded");
            self.stream
                .get_mut()
                .set_read_timeout(Some(remaining))
                .expect("set response deadline");
            let bytes = match self.stream.fill_buf() {
                Ok(bytes) => bytes,
                // Unix may report a blocking receive timeout as WouldBlock.
                Err(error)
                    if matches!(
                        error.kind(),
                        ErrorKind::Interrupted | ErrorKind::WouldBlock | ErrorKind::TimedOut
                    ) =>
                {
                    continue;
                }
                Err(error) => panic!("read MCP response: {error}"),
            };
            if bytes.is_empty() {
                assert!(!line.is_empty(), "server closed the connection");
                break;
            }
            let newline = bytes.iter().position(|&byte| byte == b'\n');
            let consumed = newline.map_or(bytes.len(), |offset| offset + 1);
            line.extend_from_slice(&bytes[..consumed]);
            self.stream.consume(consumed);
            if newline.is_some() {
                break;
            }
        }
        serde_json::from_slice(&line).expect("parse response")
    }

    pub(crate) fn request(&mut self, method: &str, params: serde_json::Value) -> serde_json::Value {
        self.next_id += 1;
        let id = self.next_id;
        self.send(&serde_json::json!({
            "jsonrpc": "2.0", "id": id, "method": method, "params": params,
        }));
        // Notifications and partial frames share the original request deadline.
        let deadline = Instant::now() + self.response_timeout;
        loop {
            let response = self.recv_until(deadline);
            if response.get("id") == Some(&serde_json::json!(id)) {
                return response;
            }
        }
    }

    pub(crate) fn initialize(&mut self) -> serde_json::Value {
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
