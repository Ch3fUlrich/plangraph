use crate::{
    errors::ToolError,
    placement::{self, parameter, Args},
};
use pg_worker_core::run::record::{new_run_id, RunRecord, Status};
use serde_json::{json, Value};
use std::path::Path;
fn save(state: &Path, host: &str, base: Value, status: Status) -> Result<Value, ToolError> {
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64();
    let id = new_run_id();
    // The saved fields themselves, not a re-parse: serde_json float parsing may drift 1 ULP.
    RunRecord::create(
        state,
        &id,
        host,
        base.as_object().unwrap().clone(),
        status,
        now,
    )
    .map(|record| record.to_value())
    .map_err(|_| ToolError::Internal("could not save run record".into()))
}
fn validate(args: &Args) -> Result<(), ToolError> {
    crate::leg_ref::check_review_role(args)?;
    if !args.contains_key("prompt") {
        return Err(ToolError::Key("prompt".into()));
    }
    if args.get("card").is_none_or(|v| {
        v.is_null()
            || v.as_object().is_some_and(|m| m.is_empty())
            || v.as_str() == Some("")
            || v.as_array().is_some_and(|a| a.is_empty())
    }) {
        return Err(parameter("spawn by card; card required"));
    }
    for (name, valid, description) in [
        ("prompt", args["prompt"].is_string(), "a string"),
        ("card", args["card"].is_object(), "an object"),
        (
            "dry_run",
            args.get("dry_run").is_none_or(Value::is_boolean),
            "a bool",
        ),
        (
            "wait_seconds",
            args.get("wait_seconds")
                .is_none_or(|v| v.as_f64().is_some_and(|n| n >= 0.0)),
            "a number >= 0",
        ),
    ] {
        if !valid {
            return Err(parameter(format!("{name} must be {description}")));
        }
    }
    if args
        .get("role")
        .is_some_and(|v| !matches!(v.as_str(), Some("implement" | "review")))
    {
        return Err(parameter("role must be implement or review"));
    }
    for option in [
        "seats",
        "model_or_combo",
        "override_reason",
        "review_scope",
        "card.findings",
    ] {
        let unsupported = match option {
            "seats" => args.get(option) == Some(&json!(true)),
            "card.findings" => {
                args.get("role").is_none_or(|role| role == "implement")
                    && args["card"].get("findings").is_some_and(truthy)
            }
            _ => args.get(option).is_some_and(|v| !v.is_null()),
        };
        if unsupported {
            return Err(parameter(format!(
                "{option} is not supported by pg-spawnd yet; use the Python spawner"
            )));
        }
    }
    placement::route_args(args)?;
    Ok(())
}
fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64() != Some(0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(m) => !m.is_empty(),
    }
}
fn record_validation_error(
    state: &Path,
    host: &str,
    role: Value,
    error: &ToolError,
) -> Result<(), ToolError> {
    let (name, message, code, status) = match error {
        ToolError::Key(key) => ("KeyError", format!("'{key}'"), 1, Status::Failed),
        ToolError::Refused {
            kind: crate::errors::RefusalKind::Parameter,
            message,
            ..
        } => ("ParameterRefused", message.clone(), 2, Status::Refused),
        _ => return Ok(()),
    };
    save(
        state,
        host,
        json!({"role":role,"refusal":name,"error":message,"exit_code":code,"legs":[],"evidence":[]}),
        status,
    )?;
    Ok(())
}
pub(crate) fn spawn(
    state: &Path,
    args: &Args,
    host: &str,
    runs: &crate::runs::Runs,
) -> Result<Value, ToolError> {
    let args = prompt_inputs(args)?;
    let args = &args;
    let role = args.get("role").cloned().unwrap_or(json!("implement"));
    let admitted = crate::leg_ref::admit(&crate::leg_ref::AdapterCatalog::new(state), args)
        .and_then(|a| validate(&a).map(|()| a));
    let admitted = match admitted {
        Ok(args) => args,
        Err(error) => {
            record_validation_error(state, host, role, &error)?;
            return Err(error);
        }
    };
    let args = &admitted;
    let plan: Value = serde_json::from_str(&placement::route(state, args)?)
        .map_err(|_| ToolError::Internal("route adapter returned invalid output".into()))?;
    let leg = &plan["first_leg"];
    if !plan["legs"].is_array()
        || !leg.is_object()
        || ["id", "harness", "model", "family"]
            .iter()
            .any(|k| !leg[k].is_string())
    {
        return Err(ToolError::Internal("route adapter returned no leg".into()));
    }
    runs.spawn(state, args, host, role, plan)
}

fn prompt_inputs(args: &Args) -> Result<Args, ToolError> {
    let mut args = args.clone();
    if let Some(path) = args.get("prompt_file") {
        let path = path.as_str().ok_or_else(|| parameter("prompt_file must be a path"))?;
        if args.contains_key("prompt") {
            return Err(parameter(format!("prompt_input_conflict: {path}")));
        }
        let text = std::fs::read_to_string(path)
            .map_err(|_| parameter(format!("prompt_file_not_readable: {path}")))?;
        args.insert("prompt".into(), json!(text.trim_start_matches('\u{feff}')));
    }
    if args.get("prompt").and_then(Value::as_str).is_some_and(|s| s.trim().is_empty()) {
        return Err(parameter("prompt must not be blank"));
    }
    if let Some(paths) = args.get("card").and_then(|c| c.get("input_files")) {
        let paths = paths.as_array().ok_or_else(|| parameter("card.input_files must be a list"))?;
        for path in paths {
            if !path.as_str().is_some_and(|p| Path::new(p).is_file()) {
                return Err(parameter(format!("input_file_not_regular: {path}")));
            }
        }
    }
    Ok(args)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn prompt_input_refusals_and_inline() {
        let dir = std::env::temp_dir().join(new_run_id());
        std::fs::create_dir(&dir).unwrap();
        let path = dir.join("input.txt");
        for args in [
            json!({"prompt":" \n"}),
            json!({"prompt_file":path}),
            json!({"prompt":"Read input", "card":{"input_files":[path]}}),
        ] {
            let start = std::time::Instant::now();
            assert!(prompt_inputs(args.as_object().unwrap()).is_err());
            assert!(start.elapsed().as_secs_f64() < 1.0);
        }
        std::fs::write(&path, "Read supplied material\n").unwrap();
        let args = prompt_inputs(json!({"prompt_file":path}).as_object().unwrap()).ok().unwrap();
        assert_eq!(args["prompt"], "Read supplied material\n");
        std::fs::remove_file(path).unwrap();
        std::fs::remove_dir(dir).unwrap();
    }
    #[test]
    fn unexpected_validation_error_is_unrecorded() {
        let state = std::env::temp_dir().join(format!("pg-spawnd-validation-{}", new_run_id()));
        let error = ToolError::Internal("validation failed".into());
        assert!(record_validation_error(&state, "local", json!("implement"), &error).is_ok());
        let refused = ToolError::Refused {
            kind: crate::errors::RefusalKind::Spawn,
            message: "no leg".into(),
            legs: vec![],
        };
        assert!(record_validation_error(&state, "local", json!("implement"), &refused).is_ok());
        let answer = error.into_error_data();
        assert_eq!(answer.message, "validation failed");
        assert!(!state.exists());
        let key = ToolError::Key("card".into());
        assert!(record_validation_error(&state, "local", json!("review"), &key).is_ok());
        let rows = crate::records::list_runs(&state);
        std::fs::remove_dir_all(&state).unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0]["refusal"], "KeyError");
        assert_eq!(rows[0]["error"], "'card'");
        assert_eq!(rows[0]["role"], "review");
    }
}
