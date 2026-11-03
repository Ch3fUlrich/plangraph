//! Shared helpers for pg-spawnd CLI integration tests.
#![allow(dead_code)] // each test binary uses a different subset

use std::os::unix::fs::{DirBuilderExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::{Duration, Instant};

static COUNTER: AtomicUsize = AtomicUsize::new(0);

/// A 0700 scratch directory removed on drop.
pub(crate) struct TempDir(PathBuf);

impl TempDir {
    pub(crate) fn new() -> TempDir {
        use std::hash::{BuildHasher, Hasher};
        let random = std::collections::hash_map::RandomState::new()
            .build_hasher()
            .finish();
        let base = std::env::temp_dir().join(format!(
            "pg-spawnd-it-{}-{}",
            random,
            COUNTER.fetch_add(1, Ordering::SeqCst)
        ));
        std::fs::DirBuilder::new()
            .mode(0o700)
            .create(&base)
            .expect("exclusive temp dir");
        TempDir(base)
    }

    pub(crate) fn path(&self) -> &Path {
        &self.0
    }
}

impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
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
        let _ = self.child.wait();
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
    let status = guard.child.wait().unwrap();
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
