use super::{safe_name, Config};
use serde_json::Value;
use std::{
    fs::{self, File},
    io::{Read, Write},
    path::Path,
};

pub const FILE_MAX: u64 = 1024 * 1024;

pub fn read_json(path: &Path) -> Option<Value> {
    let mut bytes = Vec::new();
    File::open(path)
        .ok()?
        .take(FILE_MAX + 1)
        .read_to_end(&mut bytes)
        .ok()?;
    if bytes.len() as u64 > FILE_MAX {
        return None;
    }
    serde_json::from_slice(&bytes).ok()
}

pub fn atomic_write(path: &Path, bytes: &[u8]) -> std::io::Result<()> {
    if bytes.len() as u64 > FILE_MAX {
        return Err(std::io::Error::other("status too large"));
    }
    let mut file = tempfile::NamedTempFile::new_in(path.parent().unwrap())?;
    file.write_all(bytes)?;
    file.as_file().sync_all()?;
    file.persist(path).map_err(|e| e.error)?;
    Ok(())
}

pub fn resolve_name(config: &Config, id: &str) -> String {
    if let Some(name) = config.session_name.as_deref().filter(|s| safe_name(s)) {
        return name.into();
    }
    if let Ok(entries) = fs::read_dir(&config.launch_dir) {
        let mut paths: Vec<_> = entries.flatten().map(|e| e.path()).collect();
        paths.sort();
        for path in paths {
            if path.extension().is_none_or(|e| e != "json") {
                continue;
            }
            let Some(record) = read_json(&path) else {
                continue;
            };
            if record["session_id"] != id {
                continue;
            }
            if let Some(name) = record["name"].as_str().filter(|s| safe_name(s)) {
                return name.into();
            }
        }
    }
    id.into()
}
