//! Publish executable test fixtures without opening the destination for writing.

use std::io::Write;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

/// Write and sync a temporary file, close it, then publish it as `dir/name`.
pub fn write_script(dir: &Path, name: &str, body: &str) -> PathBuf {
    let destination = dir.join(name);
    let (mut file, temporary) = tempfile::NamedTempFile::new_in(dir).unwrap().into_parts();
    file.write_all(body.as_bytes()).unwrap();
    file.set_permissions(std::fs::Permissions::from_mode(0o755))
        .unwrap();
    file.sync_all().unwrap();
    drop(file);
    temporary.persist(&destination).unwrap();
    destination
}
