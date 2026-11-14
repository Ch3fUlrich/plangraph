//! Offline plan-quality report; never executes card commands.
use pg_worker_core::plan_lint::lint_snapshot;
use serde_json::Value;
use std::{env, error::Error, fs, process::ExitCode};
fn run() -> Result<bool, Box<dyn Error>> {
    let mut args = env::args().skip(1);
    let (mut source, mut json) = (None, false);
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--help" | "-h" => {
                println!("pg-plan-lint --snapshot FILE [--json]; commands are never executed.");
                return Ok(true);
            }
            "--snapshot" => source = Some(args.next().ok_or("--snapshot needs a file")?),
            "--json" => json = true,
            _ => return Err(format!("unknown argument: {arg}").into()),
        }
    }
    let raw = fs::read_to_string(source.ok_or("usage: pg-plan-lint --snapshot FILE [--json]")?)?;
    let snapshot: Value = serde_json::from_str(&raw)?;
    let items = snapshot.as_array().or_else(|| snapshot["items"].as_array());
    if !items.is_some_and(|a| a.iter().all(Value::is_object)) {
        return Err("snapshot needs an array of item objects".into());
    }
    let report = lint_snapshot(&snapshot);
    if json {
        println!("{}", serde_json::to_string_pretty(&report)?);
    } else {
        for r in &report {
            let codes = r.codes.join(",");
            println!("{} score={}/10 codes={codes}", r.message, r.score);
        }
    }
    Ok(report.iter().all(|r| r.codes.is_empty()))
}
fn main() -> ExitCode {
    match run() {
        Ok(clean) => ExitCode::from(u8::from(!clean)),
        Err(e) => {
            eprintln!("pg-plan-lint: {e}");
            ExitCode::from(2)
        }
    }
}
