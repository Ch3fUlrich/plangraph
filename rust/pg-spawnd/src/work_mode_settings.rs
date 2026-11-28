//! User work-mode settings over the spawner's existing MCP transport.
use crate::{errors::ToolError, placement::parameter};
use pg_worker_core::{run::record_io::atomic_write_private, work_mode::WorkMode};
use serde::Deserialize;
use serde_json::{json, Value};
use std::path::{Path, PathBuf};

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Request {
    repo: Option<PathBuf>,
    // Keep absence distinct from an explicit null, which must be refused.
    #[serde(default)]
    work_mode: Option<Value>,
}

pub(crate) fn settings(
    state: &Path,
    args: &serde_json::Map<String, Value>,
) -> Result<String, ToolError> {
    let request: Request = serde_json::from_value(Value::Object(args.clone()))
        .map_err(|_| parameter("expected optional repo and work_mode settings"))?;
    let requested = if args.contains_key("work_mode") {
        Some(
            serde_json::from_value::<WorkMode>(request.work_mode.unwrap_or(Value::Null))
                .map_err(|_| parameter("work_mode must be auto, piecewise or batch"))?,
        )
    } else {
        None
    };
    let repo = match request.repo {
        Some(path) if path.is_absolute() && path.is_dir() => path,
        Some(_) => return Err(parameter("repo must be an existing absolute directory")),
        None => std::env::current_dir().map_err(|e| ToolError::Internal(e.to_string()))?,
    };
    // Reject malformed repository configuration before changing user preferences.
    crate::work_mode_config::configured_mode(&repo).map_err(parameter)?;
    if let Some(mode) = requested {
        atomic_write_private(
            &crate::prompt::config_root().join("work-mode.json"),
            json!({"work_mode":mode}).to_string().as_bytes(),
        )
        .map_err(|_| ToolError::Internal("could not save work mode configuration".into()))?;
    }
    let mode = crate::work_mode_config::user_mode().map_err(parameter)?;
    let effective = crate::work_mode_config::configured_mode(&repo).map_err(parameter)?;
    let mut result = json!({"work_mode":mode,"effective_work_mode":effective});
    if effective == WorkMode::Auto {
        let route_args = json!({"card":{"repo":repo,"estimate_tokens":1},"role":"implement"});
        match crate::placement::route(state, route_args.as_object().unwrap()) {
            Ok(text) => {
                let route: Value = serde_json::from_str(&text)
                    .map_err(|_| ToolError::Internal("invalid work mode route".into()))?;
                result["resolved_work_mode"] = route["work_mode"].clone();
                result["work_mode_reason"] = route["work_mode_reason"].clone();
            }
            // A saved setting remains readable even when routing is unavailable.
            Err(_) => {
                result["resolved_work_mode"] = Value::Null;
                result["work_mode_reason"] = json!("Auto resolution unavailable");
            }
        }
    } else {
        let resolved = pg_worker_core::work_mode_auto::resolve(effective, &[]);
        result["resolved_work_mode"] = json!(resolved.work_mode);
        result["work_mode_reason"] = json!(resolved.work_mode_reason);
    }
    Ok(result.to_string())
}
