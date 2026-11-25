//! Local state for host disk alerts: the hygiene watermark file and the watchdog's own record of
//! when it last saw each filesystem recover.
use super::key::Key;
use std::{collections::BTreeMap, fs, io, path::Path};

const RECOVERIES: &str = "disk-recovered.json";

/// Seconds since the epoch; `PLANGRAPH_WATCHDOG_NOW` fixes it for tests.
pub fn now() -> u64 {
    std::env::var("PLANGRAPH_WATCHDOG_NOW")
        .ok()
        .and_then(|value| value.parse().ok())
        .unwrap_or_else(|| {
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap_or_default()
                .as_secs()
        })
}

/// `disk-watermarks.json`, keyed by device: `PLANGRAPH_DISK_WATERMARKS`, else the spawn state.
#[cfg(unix)]
pub fn watermarks() -> Option<serde_json::Map<String, serde_json::Value>> {
    let var = |name| std::env::var_os(name).filter(|v| !v.is_empty());
    let path = var("PLANGRAPH_DISK_WATERMARKS")
        .map(std::path::PathBuf::from)
        .or_else(|| {
            var("PLANGRAPH_SPAWN_STATE")
                .map(std::path::PathBuf::from)
                .or_else(|| {
                    var("HOME")
                        .map(|home| std::path::PathBuf::from(home).join(".local/state/plangraph"))
                })
                .map(|state| state.join("disk-watermarks.json"))
        })?;
    match serde_json::from_slice(&fs::read(path).ok()?).ok()? {
        serde_json::Value::Object(map) => Some(map),
        _ => None,
    }
}

/// Crossing key -> the time the watchdog last observed that filesystem recover.
pub fn recoveries(state: &Path) -> BTreeMap<String, u64> {
    fs::read(state.join(RECOVERIES))
        .ok()
        .and_then(|bytes| serde_json::from_slice(&bytes).ok())
        .unwrap_or_default()
}

pub fn record_recoveries(state: &Path, crossings: &[Key], now: u64) -> io::Result<()> {
    if crossings.is_empty() {
        return Ok(());
    }
    let mut all = recoveries(state);
    for key in crossings {
        all.insert(key.render(), now);
    }
    let path = state.join(RECOVERIES);
    let tmp = path.with_extension("tmp");
    fs::write(&tmp, serde_json::to_vec(&all)?)?;
    fs::rename(tmp, path)
}
