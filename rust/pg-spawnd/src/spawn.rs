use crate::{
    errors::ToolError,
    placement::{self, parameter, Args},
};
use pg_worker_core::run::record::{new_run_id, RunRecord, Status};
use serde_json::{json, Value};
use std::path::Path;
fn save(state: &Path, base: Value, status: Status) -> Result<Value, ToolError> {
    let host = std::env::var("PLANGRAPH_HOST")
        .ok()
        .filter(|s| {
            (1..=32).contains(&s.len())
                && s.as_bytes()[0].is_ascii_lowercase()
                && s.bytes()
                    .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-')
        })
        .unwrap_or_else(|| "local".into());
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64();
    let id = new_run_id();
    RunRecord::create(
        state,
        &id,
        &host,
        base.as_object().unwrap().clone(),
        status,
        now,
    )
    .map_err(|_| ToolError::Internal("could not save run record".into()))?;
    crate::records::read_record(state, &json!(id))
}
fn validate(args: &Args) -> Result<(), ToolError> {
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
            "card.findings" => args["card"].get("findings").is_some_and(|v| {
                !v.is_null()
                    && v.as_array().is_none_or(|a| !a.is_empty())
                    && v.as_object().is_none_or(|m| !m.is_empty())
                    && v.as_str() != Some("")
            }),
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
pub(crate) fn spawn(state: &Path, args: &Args) -> Result<Value, ToolError> {
    let role = args.get("role").cloned().unwrap_or(json!("implement"));
    if let Err(error) = validate(args) {
        let (name, message, code, status) = match &error {
            ToolError::Key(_) => ("KeyError", "'prompt'".to_string(), 1, Status::Failed),
            ToolError::Refused { message, .. } => {
                ("ParameterRefused", message.clone(), 2, Status::Refused)
            }
            _ => unreachable!(),
        };
        save(
            state,
            json!({"role":role,"refusal":name,"error":message,"exit_code":code,"legs":[],"evidence":[]}),
            status,
        )?;
        return Err(error);
    }
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
    if args.get("dry_run") != Some(&json!(true)) {
        return Err(ToolError::Internal(
            "launching is not available yet (R6a-3c)".into(),
        ));
    }
    let tail = format!(
        "[dry-run] Planned leg: {} via {} {}",
        leg["id"].as_str().unwrap(),
        leg["harness"].as_str().unwrap(),
        leg["model"].as_str().unwrap()
    );
    let cwd = std::env::current_dir()
        .map_err(|_| ToolError::Internal("could not read current directory".into()))?;
    save(
        state,
        json!({"card_hash":crate::pyjson::card_hash(&args["card"]),"role":role,"plan":plan,"chosen_leg":leg,"request_side_model":null,"witness_kind":null,"writer_family":leg["family"],"served_model":leg["model"],"served_family":leg["family"],"cwd":cwd.to_string_lossy(),"exit_code":0,"stdout_tail":tail,"attempts":[{"leg":leg,"status":"dry_run","served_model":leg["model"],"served_family":leg["family"],"served_witness_reason":"no authenticated harness telemetry; skipped or unobserved leg","request_side_model":null,"witness_kind":null,"skip_reason":null,"provider_response":null,"stdout_tail":tail,"stderr_tail":""}]}),
        Status::DryRun,
    )
}
