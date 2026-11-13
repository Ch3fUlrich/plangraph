//! Copy-once import of non-secret legacy runtime state. Never overwrite new state.
use crate::atomic;
use std::fs;
use std::io;
use std::path::Path;
pub fn relocated(old: &Path, new: &Path) -> io::Result<bool> {
    if !old.exists() || !new.exists() {
        return Ok(false);
    }
    Ok(fs::canonicalize(old)? == fs::canonicalize(new)?)
}
pub fn migration_lock(root: &Path) -> io::Result<fs::File> {
    fs::create_dir_all(root)?;
    atomic::lock(&root.join("namespace-migration"))
}
pub fn breaker(old: &Path, new: &Path, dry_run: bool) -> io::Result<usize> {
    if !old.exists() {
        return Ok(0);
    }
    if relocated(old, new)? {
        return Ok(0);
    }
    if old.symlink_metadata()?.file_type().is_symlink() || !old.is_dir() {
        return Err(io::Error::other("invalid source"));
    }
    if dry_run {
        return Ok(fs::read_dir(old)?.count());
    }
    let _migration_lock = migration_lock(
        new.parent()
            .ok_or_else(|| io::Error::other("missing state root"))?,
    )?;
    fs::create_dir_all(new)?;
    let marker = new.join(".autoos-imported");
    let _lock = atomic::lock(&marker)?;
    if marker.exists() {
        return Ok(0);
    }
    let mut count = 0;
    for entry in fs::read_dir(old)? {
        let entry = entry?;
        if !entry.file_type()?.is_file() {
            continue;
        }
        let name = entry.file_name();
        let name = name
            .to_str()
            .ok_or_else(|| io::Error::other("invalid name"))?;
        if !name.ends_with(".json") && !name.ends_with(".tripped") {
            continue;
        }
        let target = new.join(name);
        let _target_lock = atomic::lock(&target)?;
        if target.exists() {
            continue;
        }
        let bytes = fs::read(entry.path())?;
        if name.ends_with(".json") {
            let _: crate::breaker::State = serde_json::from_slice(&bytes)?;
        }
        atomic::write(&target, &bytes)?;
        count += 1;
    }
    atomic::write(
        &marker,
        b"legacy state imported; source retained for rollback\n",
    )?;
    Ok(count)
}
