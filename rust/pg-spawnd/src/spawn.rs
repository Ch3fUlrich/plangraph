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
    match runs.spawn(state, args, host, role.clone(), plan.clone()) {
        Err(error) => {
            let named = ["prompt_too_large", "stdin_prompt_flag_missing"];
            let reason = match &error {
                ToolError::Refused { message, .. } => named
                    .into_iter()
                    .find(|name| message.contains(&format!("{name}:"))),
                _ => None,
            };
            let Some(reason) = reason else {
                return Err(error);
            };
            // LaunchAdapter only maps exit 2 to Refused; retain that error's status.
            let error = error.into_error_data();
            let mut base = error.data.unwrap_or_else(|| json!({}));
            base["role"] = role;
            base["reason"] = json!(reason);
            base["error"] = json!(error.message);
            base["stderr_tail"] = base["error"].clone();
            base["stdout_tail"] = json!("");
            base["card_hash"] = json!(crate::pyjson::card_hash(&args["card"]));
            base["plan"] = plan;
            base["chosen_leg"] = Value::Null;
            base["writer_family"] = base["plan"]["first_leg"]["family"].clone();
            base["attempts"] = json!([{
                "leg":base["plan"]["first_leg"],"status":"failed","skip_reason":reason,
                "served_model":"unwitnessed","served_family":"unwitnessed",
                "served_witness_reason":"leg refused before launch",
                "request_side_model":null,"witness_kind":null,"provider_response":null,
                "stdout_tail":base["stdout_tail"],"stderr_tail":base["stderr_tail"]
            }]);
            save(state, host, base, Status::Failed)
        }
        result => result,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
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
