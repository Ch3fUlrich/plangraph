//! Sizes of a cache tree. `allocated_bytes` is the unit of the Cargo cap in every language.
//!
//! Matches `cargo_target_size_gb` in the spawner: regular files only, directory symlinks never
//! followed, each hard-linked file counted once, files that vanish mid-walk ignored.
use std::{collections::HashSet, fs, io, os::unix::fs::MetadataExt, path::Path};

/// Apparent bytes, the unit of the generic cache levels.
pub fn apparent_bytes(path: &Path) -> io::Result<u64> {
    crate::disk_hygiene::scan(path).map(|(bytes, _)| bytes)
}

pub fn allocated_bytes(path: &Path) -> io::Result<u64> {
    let mut seen = HashSet::new();
    let mut bytes = 0u64;
    let mut pending = vec![path.to_path_buf()];
    while let Some(directory) = pending.pop() {
        for entry in fs::read_dir(&directory)? {
            let entry = entry?;
            let Ok(meta) = fs::symlink_metadata(entry.path()) else {
                continue;
            };
            if meta.is_dir() {
                pending.push(entry.path());
            } else if meta.is_file() && (meta.nlink() < 2 || seen.insert((meta.dev(), meta.ino())))
            {
                bytes = bytes.saturating_add(meta.blocks().saturating_mul(512));
            }
        }
    }
    Ok(bytes)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_one_byte_file_occupies_its_allocated_block() {
        let tmp = tempfile::tempdir().unwrap();
        fs::write(tmp.path().join("one"), b"x").unwrap();
        fs::hard_link(tmp.path().join("one"), tmp.path().join("again")).unwrap();
        std::os::unix::fs::symlink(tmp.path(), tmp.path().join("loop")).unwrap();
        let bytes = allocated_bytes(tmp.path()).unwrap();
        assert!(bytes >= 512 && bytes < 1_000_000, "{bytes}");
        assert_eq!(
            bytes,
            fs::metadata(tmp.path().join("one")).unwrap().blocks() * 512
        );
    }

    #[test]
    fn a_missing_root_is_not_found() {
        let tmp = tempfile::tempdir().unwrap();
        let error = allocated_bytes(&tmp.path().join("absent")).unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::NotFound);
    }
}
