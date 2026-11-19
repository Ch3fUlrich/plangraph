//! Probe-time alert for a missing ai-stack manage.key; never reads key bytes and never refuses calls.
use pg_worker_core::run::record_io::atomic_write_private;
use serde_json::{json, Value};
use std::{
    collections::BTreeMap,
    ffi::OsString,
    fs,
    io::{self, Write},
    path::{Path, PathBuf},
};

const HELP: &str = "Usage: pg-spawnd check-manage-key [--state PATH] [--key-file PATH] [--json]";

pub(crate) fn resolve_key_path(
    flag: Option<&str>,
    env_lookup: impl Fn(&str) -> Option<OsString>,
) -> Result<PathBuf, String> {
    let env = |key| env_lookup(key).filter(|v| !v.is_empty()).map(PathBuf::from);
    if let Some(path) = flag.filter(|v| !v.is_empty()) {
        return Ok(path.into());
    }
    if let Some(dir) = env("AUTOOS_AI_STACK_CONFIG") {
        return Ok(dir.join("manage.key"));
    }
    if let Some(xdg) = env("XDG_CONFIG_HOME") {
        return Ok(xdg.join("autoos/ai-stack/manage.key"));
    }
    let home_key = if cfg!(windows) { "USERPROFILE" } else { "HOME" };
    env(home_key)
        .map(|p| p.join(".config/autoos/ai-stack/manage.key"))
        .ok_or_else(|| "cannot resolve manage.key path".into())
}

pub(crate) fn key_present(path: &Path) -> bool {
    fs::symlink_metadata(path).is_ok_and(|meta| meta.file_type().is_file() && meta.len() > 0)
}

pub(crate) fn check(state: &Path, key_path: &Path) -> Result<Value, String> {
    let alert_dir = state.join("alerts");
    let alert_path = alert_dir.join("manage-key.json");
    let key_display = key_path.to_string_lossy();
    if key_present(key_path) {
        match fs::remove_file(&alert_path) {
            Ok(()) => {}
            Err(e) if e.kind() == io::ErrorKind::NotFound => {}
            Err(e) => return Err(e.to_string()),
        }
        return Ok(json!({
            "status": "ok",
            "emitted": false,
            "alert": null,
            "key_path": key_display,
            "count": 0
        }));
    }
    let already_alerted = fs::read(&alert_path)
        .ok()
        .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok())
        .is_some_and(|v| v["alert"] == "missing_manage_key");
    if already_alerted {
        return Ok(json!({
            "status": "alert",
            "emitted": false,
            "alert": "missing_manage_key",
            "key_path": key_display,
            "count": 1
        }));
    }
    let payload = json!({
        "alert": "missing_manage_key",
        "key_path": key_display,
        "count": 1
    });
    atomic_write_private(&alert_path, payload.to_string().as_bytes())
        .map_err(|e| e.to_string())?;
    Ok(json!({
        "status": "alert",
        "emitted": true,
        "alert": "missing_manage_key",
        "key_path": key_display,
        "count": 1
    }))
}

pub(crate) fn execute(args: Vec<String>, out: &mut dyn Write) -> Result<(), String> {
    if args.first().is_some_and(|a| a == "--help" || a == "-h") {
        return writeln!(out, "{HELP}").map_err(|e| e.to_string());
    }
    let mut args = args.into_iter();
    let mut flags = BTreeMap::new();
    while let Some(flag) = args.next() {
        if flags.contains_key(&flag) {
            return Err("duplicate check-manage-key flag".into());
        }
        let value = match flag.as_str() {
            "--json" => String::new(),
            "--state" | "--key-file" => args
                .next()
                .filter(|a| !a.starts_with("--"))
                .ok_or("missing check-manage-key flag value")?,
            _ => return Err("unknown check-manage-key flag".into()),
        };
        flags.insert(flag, value);
    }
    let state = crate::state::resolve_state(flags.get("--state").map(String::as_str), |k| {
        std::env::var_os(k)
    })?;
    let key_path = resolve_key_path(flags.get("--key-file").map(String::as_str), |k| {
        std::env::var_os(k)
    })?;
    let result = check(&state, &key_path)?;
    if flags.contains_key("--json") {
        writeln!(out, "{result}").map_err(|e| e.to_string())?;
    } else if result["emitted"] == true {
        writeln!(
            out,
            "ALERT missing_manage_key: key file missing at {}",
            result["key_path"].as_str().unwrap_or("")
        )
        .map_err(|e| e.to_string())?;
    }
    Ok(())
}

#[cfg(test)]
#[path = "manage_key_alert_tests.rs"]
mod tests;
