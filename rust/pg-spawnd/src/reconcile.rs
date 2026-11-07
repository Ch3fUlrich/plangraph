//! Restart inventory. Mutating recovery remains unavailable until adoption is wired.
use serde_json::Value;
use std::path::Path;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Mode {
    Off,
    Shadow,
    On,
}

#[derive(Debug)]
pub(crate) enum ReconcileError {
    Invalid,
    Io,
    Unknown,
}
impl std::fmt::Display for ReconcileError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(match self {
            Self::Invalid => "invalid reconciliation input",
            Self::Io => "reconciliation I/O failure",
            Self::Unknown => "reconciliation authority unavailable",
        })
    }
}

pub(crate) fn mode(env: Option<&str>, config: &Value, role: &str) -> Result<Mode, ReconcileError> {
    if !config.is_null() && !config.is_object() {
        return Err(ReconcileError::Invalid);
    }
    if !["implement", "review", "orchestrate"].contains(&role) {
        return Err(ReconcileError::Invalid);
    }
    let value = env.map(Value::from).or_else(|| {
        config
            .get("roles")
            .and_then(|v| v.get(role))
            .and_then(|v| v.get("reconcile"))
            .or_else(|| config.get("reconcile"))
            .cloned()
    });
    match value.as_ref().map(Value::as_str) {
        None | Some(Some("off")) => Ok(Mode::Off),
        Some(Some("shadow")) => Ok(Mode::Shadow),
        Some(Some("on")) => Ok(Mode::On),
        _ => Err(ReconcileError::Invalid),
    }
}

#[derive(Debug, PartialEq, Eq)]
pub(crate) struct WouldOrphan {
    pub run_id: String,
    pub attempt: u32,
}

pub(crate) fn startup(
    root: &Path,
    env: Option<&str>,
    config: &Value,
) -> Result<Vec<WouldOrphan>, ReconcileError> {
    let modes = ["implement", "review", "orchestrate"].map(|role| mode(env, config, role));
    let modes: Vec<_> = modes.into_iter().collect::<Result<_, _>>()?;
    // Never offer a partially enabled mutating implementation.
    if modes.contains(&Mode::On) {
        return Err(ReconcileError::Unknown);
    }
    if modes.iter().all(|m| *m == Mode::Off) {
        return Ok(Vec::new());
    }
    let mut diagnostics = Vec::new();
    for row in crate::reconcile_store::inventory(root)? {
        if mode(env, config, &row.role)? != Mode::Shadow {
            continue;
        }
        // A recorded identity is not proof of liveness. Do not infer would_kill
        // or would_reattach from an unverified PID or arbitrary group string.
        let mut gone = row.identity.is_none();
        #[cfg(target_os = "linux")]
        if let Some(identity) = &row.identity {
            gone = matches!(std::fs::symlink_metadata(format!("/proc/{}", identity.pid)),
                Err(e) if e.kind() == std::io::ErrorKind::NotFound);
        }
        if gone {
            eprintln!("would_orphan {} {}", row.run_id, row.attempt);
            diagnostics.push(WouldOrphan {
                run_id: row.run_id,
                attempt: row.attempt,
            });
        }
    }
    Ok(diagnostics)
}

pub(crate) fn configured_startup(root: &Path) -> Result<(), String> {
    let env = std::env::var("PLANGRAPH_RELIABILITY_RECONCILE")
        .map(Some)
        .or_else(|e| match e {
            std::env::VarError::NotPresent => Ok(None),
            _ => Err(ReconcileError::Invalid),
        })
        .map_err(|e| e.to_string())?;
    // An explicit off must not inspect state or configuration files.
    if env.as_deref() == Some("off") {
        return Ok(());
    }
    let home = std::env::var_os("XDG_CONFIG_HOME")
        .map(std::path::PathBuf::from)
        .or_else(|| {
            std::env::var_os(if cfg!(windows) { "APPDATA" } else { "HOME" }).map(|p| {
                if cfg!(windows) {
                    p.into()
                } else {
                    std::path::PathBuf::from(p).join(".config")
                }
            })
        });
    let config = match home.map(|p| std::fs::read(p.join("plangraph/reliability.json"))) {
        None => Value::Null,
        Some(Err(e)) if e.kind() == std::io::ErrorKind::NotFound => Value::Null,
        Some(Ok(bytes)) => {
            serde_json::from_slice(&bytes).map_err(|_| ReconcileError::Invalid.to_string())?
        }
        Some(Err(_)) => return Err(ReconcileError::Io.to_string()),
    };
    startup(root, env.as_deref(), &config)
        .map(|_| ())
        .map_err(|e| e.to_string())
}

#[cfg(test)]
#[path = "reconcile_tests.rs"]
mod tests;
