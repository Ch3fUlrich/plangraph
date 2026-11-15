use crate::{
    adapter::Adapter,
    errors::{RefusalKind, ToolError},
};
use serde_json::{Map, Value};
use std::{path::Path, time::Duration};
pub(crate) type Args = Map<String, Value>;
pub(crate) fn parameter(message: impl Into<String>) -> ToolError {
    ToolError::Refused {
        kind: RefusalKind::Parameter,
        message: message.into(),
        legs: vec![],
    }
}
fn strings(args: &Args, key: &str, flag: &str, out: &mut Vec<String>) -> Result<(), ToolError> {
    if let Some(value) = args.get(key) {
        let values = value
            .as_array()
            .filter(|v| v.iter().all(Value::is_string))
            .ok_or_else(|| parameter(format!("{key} must be an array of strings")))?;
        for value in values {
            out.extend([flag.into(), value.as_str().unwrap().into()]);
        }
    }
    Ok(())
}
fn boolean(args: &Args, key: &str, flag: &str, out: &mut Vec<String>) -> Result<(), ToolError> {
    if let Some(value) = args.get(key) {
        if value
            .as_bool()
            .ok_or_else(|| parameter(format!("{key} must be a bool")))?
        {
            out.push(flag.into());
        }
    }
    Ok(())
}
pub(crate) fn run(
    state: &Path,
    argv: Vec<String>,
    timeout: Duration,
    probe: bool,
) -> Result<String, ToolError> {
    let (code, bytes, line) = Adapter(state).run(&argv, timeout)?;
    let object = serde_json::from_slice::<Value>(&bytes)
        .ok()
        .filter(Value::is_object);
    let printed = || String::from_utf8_lossy(&bytes).trim().to_owned();
    match code {
        0 => object
            .map(|_| printed())
            .ok_or_else(|| ToolError::Internal("route adapter returned invalid output".into())),
        1 if probe => object.map(|_| printed()).ok_or_else(|| parameter(line)),
        1 => Err(ToolError::Value(line)),
        2 | 3 => Err(ToolError::Refused {
            kind: if code == 2 {
                RefusalKind::Spawn
            } else {
                RefusalKind::Benchmark
            },
            message: if line.is_empty() {
                "spawn refused".into()
            } else {
                line
            },
            legs: vec![],
        }),
        n => Err(ToolError::Internal(format!(
            "route adapter failed (exit {n})"
        ))),
    }
}
pub(crate) fn route_args(args: &Args) -> Result<Vec<String>, ToolError> {
    crate::leg_ref::check_review_role(args)?;
    let card = args
        .get("card")
        .ok_or_else(|| ToolError::Key("card".into()))?
        .to_string();
    let role = args
        .get("role")
        .and_then(Value::as_str)
        .unwrap_or(if args.contains_key("role") {
            ""
        } else {
            "implement"
        });
    if !["implement", "review", "orchestrate"].contains(&role) {
        return Err(parameter("role must be implement, review or orchestrate"));
    }
    let mut argv = vec![
        "route".into(),
        "--card".into(),
        card.clone(),
        "--role".into(),
        role.into(),
    ];
    strings(args, "writer_families", "--writer-family", &mut argv)?;
    if let Some(value) = args.get("review_of") {
        argv.extend([
            "--review-of".into(),
            value
                .as_str()
                .ok_or_else(|| parameter("review_of must be a string"))?
                .into(),
        ]);
    }
    boolean(args, "require_bench", "--require-bench", &mut argv)?;
    if let Some(value) = args.get("bench_db") {
        argv.extend([
            "--bench-db".into(),
            value
                .as_str()
                .ok_or_else(|| parameter("bench_db must be a string"))?
                .into(),
        ]);
    }
    if card.len() > 24000 {
        return Err(parameter("card too large for the route adapter"));
    }
    argv.push("--json".into());
    Ok(argv)
}
pub(crate) fn route(state: &Path, args: &Args) -> Result<String, ToolError> {
    let args = &crate::leg_ref::admit(&crate::leg_ref::AdapterCatalog::new(state), args)?;
    crate::cli_review::select(
        args,
        run(state, route_args(args)?, Duration::from_secs(30), false)?,
    )
}
pub(crate) fn probe(state: &Path, args: &Args) -> Result<String, ToolError> {
    crate::leg_ref::check_review_role(args)?;
    let mut argv = vec!["probe".into()];
    strings(args, "leg", "--leg", &mut argv)?;
    boolean(args, "all_writers", "--all-writers", &mut argv)?;
    let mut timeout = 30.0;
    if let Some(value) = args.get("timeout") {
        let n = value
            .as_f64()
            .filter(|n| *n > 0.0)
            .ok_or_else(|| parameter("timeout must be a number > 0"))?;
        timeout = (n + 15.0).clamp(30.0, 600.0);
        argv.extend(["--timeout".into(), value.to_string()]);
    }
    argv.push("--json".into());
    run(state, argv, Duration::from_secs_f64(timeout), true)
}

/// Native cards share Python's existing RAM/SSH admission, then one Rust lifecycle.
pub(crate) fn native_spawn(state: &Path, args: &Args) -> Result<Value, ToolError> {
    crate::spawn::validate(args)?;
    if !args["card"]
        .get("repo")
        .and_then(Value::as_str)
        .is_some_and(|repo| !repo.trim().is_empty() && Path::new(repo).is_absolute())
    {
        return Err(parameter("native-codex requires an absolute card.repo"));
    }
    if args.get("dry_run") == Some(&Value::Bool(true)) {
        return Err(parameter("native-codex dry_run is not supported"));
    }
    if args.get("cwd").is_some_and(|v| !v.is_null())
        || args["card"].get("cwd").is_some()
        || args.get("run_worktree") == Some(&Value::Bool(true))
    {
        return Err(parameter("native-codex owns its worktree; use card.repo"));
    }
    if args.get("role").is_some_and(|role| role != "implement") {
        return Err(parameter("native-codex requires implement role"));
    }
    let prompt = args
        .get("prompt")
        .and_then(Value::as_str)
        .ok_or_else(|| parameter("prompt must be a string"))?;
    let mut argv = route_args(args)?;
    argv[0] = "spawn".into();
    argv.extend(["--".into(), prompt.into()]);
    if let Some(wait) = args.get("wait_seconds") {
        if !wait.as_f64().is_some_and(|n| n >= 0.0) {
            return Err(parameter("wait_seconds must be a number >= 0"));
        }
        // The adapter only waits for base admission; callers poll the durable record.
    }
    native_call(state, &argv, Duration::from_secs(200))
}
fn native_call(state: &Path, argv: &[String], timeout: Duration) -> Result<Value, ToolError> {
    let (code, bytes, line) = Adapter(state).run(argv, timeout)?;
    serde_json::from_slice::<Value>(&bytes)
        .ok()
        .filter(|record| record["run_id"].is_string())
        .ok_or_else(|| match code {
            2 | 3 => ToolError::Refused {
                kind: if code == 2 {
                    RefusalKind::Spawn
                } else {
                    RefusalKind::Benchmark
                },
                message: if line.is_empty() {
                    "spawn refused".into()
                } else {
                    line
                },
                legs: vec![],
            },
            1 => ToolError::Value(line),
            _ => ToolError::Internal(format!("native operation returned no run record: {line}")),
        })
}
/// The Python owner already handles target-host refresh and proven cancellation.
pub(crate) fn native_control(state: &Path, record: &Value, verb: &str) -> Result<Value, ToolError> {
    if !["result", "cancel"].contains(&verb) {
        return Err(parameter("invalid native control verb"));
    }
    let id = record["run_id"]
        .as_str()
        .filter(|id| pg_worker_core::run::record::valid_run_id(id))
        .ok_or_else(|| parameter("invalid native run id"))?;
    let result = native_call(state, &[verb.into(), id.into()], Duration::from_secs(30))?;
    if result["run_id"] != id || result["run_kind"] != "native-codex" {
        return Err(ToolError::Internal(
            "native owner returned a different run".into(),
        ));
    }
    Ok(result)
}
