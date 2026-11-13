//! Scheduled probes use the existing isolated harness bridge and shared leg-state store.
use serde_json::{json, Value};
use std::{io::Write, path::Path, time::Duration};

pub(crate) fn key_alert(state: &Path, key: &Path) -> Result<(), String> {
    let stamp = state.join("watchdog/review-manage-key.alerted");
    if key.is_file() && std::fs::metadata(key).is_ok_and(|m| m.len() > 0) {
        let _ = std::fs::remove_file(stamp);
        return Ok(());
    }
    if stamp.exists() {
        return Ok(());
    }
    let event = json!({"rule":"review-manage-key-missing","kind":"alert",
        "recipient":"routing","reason":"review probe: manage.key missing"});
    let write = |path: &Path, bytes: &[u8]| {
        pg_worker_core::run::record_io::atomic_write_private(path, bytes).map_err(|e| e.to_string())
    };
    write(&state.join("watchdog/outbox/review-manage-key-missing.json"), event.to_string().as_bytes())?;
    write(&stamp, b"alerted")
}

pub(crate) fn execute(args: Vec<String>, out: &mut dyn Write) -> Result<(), String> {
    if !args.is_empty() {
        return Err("usage: pg-spawnd review-probe (configuration through environment)".into());
    }
    let state = crate::state::resolve_state(None, |key| std::env::var_os(key))?;
    let config = std::env::var_os("AUTOOS_AI_STACK_CONFIG").map(std::path::PathBuf::from)
        .or_else(|| std::env::var_os("XDG_CONFIG_HOME").map(|p| Path::new(&p).join("autoos/ai-stack")))
        .or_else(|| std::env::var_os("HOME").map(|p| Path::new(&p).join(".config/autoos/ai-stack")))
        .ok_or("review probe config root missing")?;
    key_alert(&state, &config.join("manage.key"))?;
    let (code, bytes, _) = crate::adapter::Adapter(&state).run(
        &["probe".into(), "--all-reviews".into(), "--json".into(), "--timeout".into(), "45".into()],
        Duration::from_secs(1800),
    ).map_err(|_| "review probe adapter unavailable")?;
    let result: Value = serde_json::from_slice(&bytes).map_err(|_| "review probe invalid result")?;
    writeln!(out, "{result}").map_err(|e| e.to_string())?;
    if code == 0 { Ok(()) } else { Err("review probe: unavailable legs booked down".into()) }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn missing_key_alert_is_once_and_rearms_after_recovery() {
        let root = std::env::temp_dir().join(pg_worker_core::run::record::new_run_id());
        let key = root.join("manage.key");
        key_alert(&root, &key).unwrap();
        let event = root.join("watchdog/outbox/review-manage-key-missing.json");
        assert!(event.is_file());
        std::fs::remove_file(&event).unwrap(); // Delivery consumes the notice.
        key_alert(&root, &key).unwrap();
        assert!(!event.exists());
        std::fs::write(&key, "fixture").unwrap();
        key_alert(&root, &key).unwrap();
        std::fs::remove_file(&key).unwrap();
        key_alert(&root, &key).unwrap();
        assert!(event.exists());
        std::fs::remove_dir_all(root).unwrap();
    }
}
