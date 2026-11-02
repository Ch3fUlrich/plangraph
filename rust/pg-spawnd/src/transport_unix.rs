//! Unix domain socket transport: path resolution, safe binding, connecting.

use std::os::unix::fs::{DirBuilderExt, MetadataExt, PermissionsExt};
use std::path::{Path, PathBuf};

extern "C" {
    fn geteuid() -> u32;
}

/// `--socket <path>` wins; otherwise `$XDG_RUNTIME_DIR/plangraph/spawnd.sock`.
pub(crate) fn resolve_socket_path(flag: Option<&str>) -> Result<PathBuf, String> {
    if let Some(path) = flag {
        return Ok(PathBuf::from(path));
    }
    match std::env::var_os("XDG_RUNTIME_DIR") {
        Some(dir) if !dir.is_empty() => {
            Ok(PathBuf::from(dir).join("plangraph").join("spawnd.sock"))
        }
        _ => Err("--socket not set and XDG_RUNTIME_DIR is not set".into()),
    }
}

fn verify_socket_dir(dir: &Path) -> Result<(), String> {
    let meta = std::fs::metadata(dir).map_err(|e| format!("cannot stat {}: {e}", dir.display()))?;
    if meta.uid() != unsafe { geteuid() } {
        return Err(format!(
            "refusing socket directory {} owned by another user",
            dir.display()
        ));
    }
    if meta.mode() & 0o777 != 0o700 {
        return Err(format!(
            "refusing socket directory {} with mode {:o} (want 700)",
            dir.display(),
            meta.mode() & 0o777
        ));
    }
    Ok(())
}

/// Bind the server socket at `path` with mode 0600.
///
/// The parent directory is created 0700 when missing and must verify as owned
/// by the current user with no group/other access before binding proceeds. A
/// stale socket (connect refused) is removed; a live server aborts the bind.
pub(crate) fn bind_listener(path: &Path) -> Result<std::os::unix::net::UnixListener, String> {
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
    verify_socket_dir(dir)?;
    if path.exists() {
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
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600))
        .map_err(|e| format!("cannot chmod socket {}: {e}", path.display()))?;
    Ok(listener)
}

pub(crate) async fn connect(path: &Path) -> Result<tokio::net::UnixStream, String> {
    tokio::net::UnixStream::connect(path)
        .await
        .map_err(|e| format!("cannot connect to {}: {e}", path.display()))
}
