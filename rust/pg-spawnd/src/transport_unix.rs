//! Unix domain socket transport: path resolution, safe binding, connecting.

use std::os::unix::fs::{DirBuilderExt, FileTypeExt, MetadataExt, PermissionsExt};
use std::path::{Path, PathBuf};

extern "C" {
    fn geteuid() -> u32;
    fn flock(fd: i32, operation: i32) -> i32;
}

/// `--socket <path>` wins; otherwise `$XDG_RUNTIME_DIR/plangraph/spawnd.sock`.
pub(crate) fn resolve_socket_path(flag: Option<&str>) -> Result<PathBuf, String> {
    if let Some(path) = flag {
        if !Path::new(path).is_absolute() {
            return Err("--socket must be an absolute path".into());
        }
        return Ok(PathBuf::from(path));
    }
    match std::env::var_os("XDG_RUNTIME_DIR") {
        Some(dir) if !dir.is_empty() => {
            Ok(PathBuf::from(dir).join("plangraph").join("spawnd.sock"))
        }
        _ => Err("--socket not set and XDG_RUNTIME_DIR is not set".into()),
    }
}

fn verify_socket_dir(dir: &Path) -> Result<(u64, u64), String> {
    let meta = std::fs::symlink_metadata(dir)
        .map_err(|e| format!("cannot stat {}: {e}", dir.display()))?;
    if !meta.is_dir() {
        return Err("socket directory must be a directory, not a symlink".into());
    }
    if meta.uid() != unsafe { geteuid() } {
        return Err(format!(
            "refusing socket directory {} owned by another user",
            dir.display()
        ));
    }
    if meta.mode() & 0o077 != 0 || meta.mode() & 0o700 != 0o700 {
        return Err(format!(
            "refusing socket directory {} with mode {:o} (want 700)",
            dir.display(),
            meta.mode() & 0o777
        ));
    }
    Ok((meta.dev(), meta.ino()))
}

/// Bind the server socket at `path` with mode 0600.
///
/// The parent directory is created 0700 when missing and must verify as owned
/// by the current user with no group/other access before binding proceeds. A
/// stale socket (connect refused) is removed; a live server aborts the bind.
pub(crate) fn bind_listener(path: &Path) -> Result<std::os::unix::net::UnixListener, String> {
    bind_checked(path, || {})
}

fn bind_checked(
    path: &Path,
    after_bind: impl FnOnce(),
) -> Result<std::os::unix::net::UnixListener, String> {
    if !path.is_absolute() {
        return Err("--socket must be an absolute path".into());
    }
    let dir = path
        .parent()
        .ok_or_else(|| format!("socket path {} has no parent directory", path.display()))?;
    if !dir.is_dir() {
        std::fs::DirBuilder::new()
            .mode(0o700)
            .recursive(true)
            .create(dir)
            .map_err(|e| format!("cannot create {}: {e}", dir.display()))?;
    }
    let identity = verify_socket_dir(dir)?;
    // Serialize probe/remove/bind across processes without leaving a lock file.
    use std::os::fd::AsRawFd;
    let directory = std::fs::File::open(dir).map_err(|e| e.to_string())?;
    if unsafe { flock(directory.as_raw_fd(), 2 | 4) } != 0 {
        return Err("already serving or binding socket directory".into());
    }
    if let Ok(meta) = std::fs::symlink_metadata(path) {
        if !meta.file_type().is_socket() {
            return Err("refusing non-socket file at socket path".into());
        }
        match std::os::unix::net::UnixStream::connect(path) {
            Ok(_) => return Err(format!("already serving on {}", path.display())),
            Err(e) if e.kind() == std::io::ErrorKind::ConnectionRefused => {
                std::fs::remove_file(path)
                    .map_err(|e| format!("cannot remove stale socket {}: {e}", path.display()))?;
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
            Err(e) => return Err(format!("cannot probe socket {}: {e}", path.display())),
        }
    }
    let listener = std::os::unix::net::UnixListener::bind(path)
        .map_err(|e| format!("cannot bind {}: {e}", path.display()))?;
    after_bind();
    if verify_socket_dir(dir) != Ok(identity) {
        let _ = std::fs::remove_file(path);
        return Err("socket directory changed during bind".into());
    }
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600))
        .map_err(|e| format!("cannot chmod socket {}: {e}", path.display()))?;
    Ok(listener)
}

pub(crate) async fn connect(path: &Path) -> Result<tokio::net::UnixStream, String> {
    let dir = path.parent().ok_or("socket path has no parent")?;
    let identity = verify_socket_dir(dir)?;
    let stream = tokio::net::UnixStream::connect(path)
        .await
        .map_err(|e| format!("cannot connect to {}: {e}", path.display()))?;
    if verify_socket_dir(dir)? != identity {
        return Err("socket directory changed during connect".into());
    }
    if stream.peer_cred().map_err(|e| e.to_string())?.uid() != unsafe { geteuid() } {
        return Err("socket peer is owned by another user".into());
    }
    Ok(stream)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn private_tempdir() -> tempfile::TempDir {
        tempfile::Builder::new()
            .permissions(std::fs::Permissions::from_mode(0o700))
            .tempdir()
            .unwrap()
    }

    #[tokio::test]
    #[ignore = "requires root to create a foreign-uid socket peer"]
    async fn foreign_uid_peer_is_refused() {
        if unsafe { geteuid() } != 0 {
            return;
        }
        extern "C" {
            fn setuid(uid: u32) -> i32;
        }
        let temp = private_tempdir();
        let base = temp.path();
        let path = base.join("socket");
        // The child switches to another uid before it binds and listens, so the
        // peer check sees the foreign uid while the verified directory stays ours.
        std::fs::set_permissions(base, std::fs::Permissions::from_mode(0o777)).unwrap();
        use std::os::unix::process::CommandExt;
        let mut command = std::process::Command::new("python3");
        command.args(["-c", "import socket,sys,time; s=socket.socket(socket.AF_UNIX); s.bind(sys.argv[1]); s.listen(); time.sleep(3)"]).arg(&path);
        unsafe {
            command.pre_exec(|| {
                if setuid(1) == 0 {
                    Ok(())
                } else {
                    Err(std::io::Error::last_os_error())
                }
            });
        }
        let mut child = command.spawn().unwrap();
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(2);
        while !path.exists() {
            assert!(std::time::Instant::now() < deadline);
            std::thread::sleep(std::time::Duration::from_millis(10));
        }
        std::fs::set_permissions(base, std::fs::Permissions::from_mode(0o700)).unwrap();
        let result = connect(&path).await;
        child.kill().unwrap();
        child.wait().unwrap();
        std::fs::remove_dir_all(base).unwrap();
        assert!(result
            .unwrap_err()
            .contains("peer is owned by another user"));
    }

    #[tokio::test]
    async fn shim_refuses_accessible_and_symlinked_directories() {
        let temp = private_tempdir();
        let base = temp.path();
        let dir = base.join("dir");
        std::fs::DirBuilder::new().mode(0o700).create(&dir).unwrap();
        let path = dir.join("socket");
        let listener = std::os::unix::net::UnixListener::bind(&path).unwrap();
        listener.set_nonblocking(true).unwrap();
        for mode in [0o750, 0o707] {
            std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(mode)).unwrap();
            assert!(connect(&path).await.unwrap_err().contains("mode"));
            assert_eq!(
                listener.accept().unwrap_err().kind(),
                std::io::ErrorKind::WouldBlock
            );
        }
        std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o700)).unwrap();
        let link = base.join("link");
        std::os::unix::fs::symlink(&dir, &link).unwrap();
        assert!(connect(&link.join("socket"))
            .await
            .unwrap_err()
            .contains("symlink"));
        std::fs::remove_dir_all(base).unwrap();
    }

    #[test]
    fn concurrent_bind_cannot_replace_live_socket() {
        let temp = private_tempdir();
        let base = temp.path();
        let path = base.join("socket");
        drop(std::os::unix::net::UnixListener::bind(&path).unwrap());
        let mut replacement = None;
        let winner = bind_checked(&path, || {
            std::fs::remove_file(&path).unwrap();
            assert!(bind_listener(&path).is_err());
            replacement = Some(std::os::unix::net::UnixListener::bind(&path).unwrap());
        })
        .unwrap();
        // Pin the foreign replacement socket, not the flock winner's socket.
        let identity = std::fs::metadata(&path).unwrap().ino();
        assert!(bind_listener(&path).is_err());
        assert_eq!(std::fs::metadata(&path).unwrap().ino(), identity);
        drop(replacement);
        drop(winner);
        std::fs::remove_dir_all(base).unwrap();
    }

    #[test]
    fn directory_identity_change_after_bind_is_refused() {
        let temp = private_tempdir();
        let base = temp.path();
        let dir = base.join("dir");
        let moved = base.join("moved");
        let result = bind_checked(&dir.join("spawnd.sock"), || {
            std::fs::rename(&dir, &moved).unwrap();
            std::fs::DirBuilder::new().mode(0o700).create(&dir).unwrap();
        });
        assert!(result.is_err());
        std::fs::remove_dir_all(base).unwrap();
    }
}
