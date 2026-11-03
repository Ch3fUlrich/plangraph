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
fn run(
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
    run(state, route_args(args)?, Duration::from_secs(30), false)
}
pub(crate) fn probe(state: &Path, args: &Args) -> Result<String, ToolError> {
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
