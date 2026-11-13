//! Import-first gate for the fixed legacy state consumers in the manifest.
use super::{present, resolve, Entry};
use std::{fs, io, path::Path};
pub(super) fn check(home: &Path, data: &Path, entries: &[Entry]) -> io::Result<()> {
    for entry in entries {
        let old = resolve(home, data, &entry.old)?;
        let new = resolve(home, data, &entry.new)?;
        if !present(&old)? || crate::migrate::relocated(&old, &new)? {
            continue;
        }
        let marker = match entry.name.as_str() {
            "breaker-state" => Some(new.join(".autoos-imported")),
            "remaining-state-inventory"
                if ["daily-gate.json", "daily-gate.status"]
                    .iter()
                    .any(|name| old.join(name).exists()) =>
            {
                Some(new.join(".daily-gate-imported"))
            }
            _ => None,
        };
        if let Some(marker) = marker {
            if !fs::symlink_metadata(marker)
                .is_ok_and(|m| m.is_file() && !m.file_type().is_symlink())
            {
                return Err(io::Error::other("run state imports before namespace apply"));
            }
        }
    }
    Ok(())
}
pub(super) fn lock(home: &Path, data: &Path, entries: &[Entry]) -> io::Result<Option<fs::File>> {
    for entry in entries {
        if entry.name == "remaining-state-inventory" {
            return crate::migrate::migration_lock(&resolve(home, data, &entry.new)?).map(Some);
        }
    }
    for entry in entries {
        if entry.name == "breaker-state" {
            let new = resolve(home, data, &entry.new)?;
            return crate::migrate::migration_lock(new.parent().unwrap()).map(Some);
        }
    }
    Ok(None)
}
