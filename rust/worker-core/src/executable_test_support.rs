//! Publish executable test fixtures atomically and retry transient busy execs.

use std::io::{self, Write};
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command};

/// Write and sync a temporary file, close it, then publish it as `dir/name`.
pub fn write_script(dir: &Path, name: &str, body: &str) -> PathBuf {
    let destination = dir.join(name);
    write_executable(&destination, body);
    destination
}

/// Publish an executable fixture at an existing directory's path.
pub fn write_executable(path: impl AsRef<Path>, body: impl AsRef<[u8]>) {
    publish(path.as_ref(), body.as_ref(), 0o755);
}

/// Publish an executable fixture readable and runnable by its owner only.
pub fn write_private_executable(path: impl AsRef<Path>, body: impl AsRef<[u8]>) {
    publish(path.as_ref(), body.as_ref(), 0o700);
}

/// Spawn a fixture, retrying while a concurrent fork still holds its write descriptor.
pub fn spawn(command: &mut Command) -> io::Result<Child> {
    crate::stall::spawn_retrying(command)
}

fn publish(path: &Path, body: &[u8], mode: u32) {
    let (mut file, temporary) = tempfile::NamedTempFile::new_in(path.parent().unwrap())
        .unwrap()
        .into_parts();
    file.write_all(body).unwrap();
    file.set_permissions(std::fs::Permissions::from_mode(mode))
        .unwrap();
    file.sync_all().unwrap();
    drop(file);
    temporary.persist(path).unwrap();
}
