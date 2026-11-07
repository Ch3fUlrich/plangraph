//! Restart inventory and settlement of proven-gone execution groups.
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
        Some(Some("on")) if env.is_some() => Ok(Mode::On),
        // Shared configuration is advisory in S1; only explicit env `on`
        // can request the unavailable mutating backend and refuse startup.
        _ if env.is_none() => Ok(Mode::Off),
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
    if modes.iter().all(|m| *m == Mode::Off) {
        return Ok(Vec::new());
    }
    let _lock = if modes.contains(&Mode::On) && root.exists() {
        Some(crate::reconcile_store::lock(
            root,
            &root.join("reconcile.lock"),
        )?)
    } else {
        None
    };
    let mut diagnostics = Vec::new();
    let mut unavailable = false;
    let now = epoch_ms().ok();
    for row in crate::reconcile_store::inventory(root)? {
        let mode = mode(env, config, &row.role)?;
        if mode == Mode::Off {
            continue;
        }
        if mode == Mode::On {
            match crate::reconcile_store::orphan_gone(root, &row)? {
                true => (),
                false => {
                    eprintln!("would_reattach {} {}", row.run_id, row.attempt);
                    unavailable = true;
                }
            }
            continue;
        }
        // Only a verified live leader can support a read-only adoption decision.
        let gone = matches!(
            crate::reconcile_store::gone(row.identity.as_ref()),
            Ok(true)
        );
        if gone {
            eprintln!("would_orphan {} {}", row.run_id, row.attempt);
            diagnostics.push(WouldOrphan {
                run_id: row.run_id,
                attempt: row.attempt,
            });
        } else if let Some(kind) = now.and_then(|now| shadow_live(&row, now)) {
            eprintln!("{kind} {} {}", row.run_id, row.attempt);
        }
    }
    if unavailable {
        Err(ReconcileError::Unknown)
    } else {
        Ok(diagnostics)
    }
}

fn epoch_ms() -> Result<u64, ReconcileError> {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .ok()
        .and_then(|d| u64::try_from(d.as_millis()).ok())
        .ok_or(ReconcileError::Invalid)
}

pub(crate) fn shadow_live(row: &crate::reconcile_store::Entry, now: u64) -> Option<&'static str> {
    shadow_decision(row.deadline_ms, now, || {
        #[cfg(target_os = "linux")]
        {
            let identity = row.identity.as_ref()?;
            let copy = crate::reconcile_store::Proc {
                pid: identity.pid,
                start_time: identity.start_time,
                group: identity.group.clone(),
                pgid: identity.pgid,
            };
            crate::reconcile_backend::Leader::open(copy).ok()?;
            Some(())
        }
        #[cfg(not(target_os = "linux"))]
        {
            let _ = (row, now);
            None
        }
    })
}

fn shadow_decision(
    deadline: Option<u64>,
    now: u64,
    verify: impl FnOnce() -> Option<()>,
) -> Option<&'static str> {
    verify()?;
    Some(if deadline.is_some_and(|deadline| now >= deadline) {
        "would_kill"
    } else {
        "would_reattach"
    })
}

pub(crate) fn configured_startup(root: &Path) -> Result<(), String> {
    let env = match std::env::var("PLANGRAPH_RELIABILITY_RECONCILE") {
        Ok(value) => Some(value),
        Err(std::env::VarError::NotPresent) => None,
        Err(std::env::VarError::NotUnicode(_)) => {
            eprintln!(
                "reconciliation startup skipped: {}",
                ReconcileError::Invalid
            );
            return Ok(());
        }
    };
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
        Some(Ok(bytes)) => serde_json::from_slice(&bytes).unwrap_or(Value::Null),
        Some(Err(_)) => Value::Null,
    };
    match startup(root, env.as_deref(), &config) {
        Ok(_) => Ok(()),
        // Only explicit env `on` may refuse startup; all other inventory
        // and mode errors are advisory (D-4659, D-4671).
        Err(error) if env.as_deref() != Some("on") => {
            eprintln!("reconciliation startup skipped: {error}");
            Ok(())
        }
        Err(error) => Err(error.to_string()),
    }
}

#[cfg(any(windows, test))]
#[path = "reconcile_job.rs"]
pub(crate) mod job;
#[cfg(test)]
#[path = "reconcile_tests.rs"]
mod tests;
