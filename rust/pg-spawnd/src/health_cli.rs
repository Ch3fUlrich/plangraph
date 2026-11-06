//! Explicit health CLI bridge; the only write is an owned baseline snapshot.
use serde_json::Value;
use std::{collections::BTreeMap, io::Write};
const HELP: &str = "health [--state PATH] --json | health report [--state PATH] --since RFC3339 --until RFC3339 [--baseline] [--json]";
pub(crate) fn execute(args: Vec<String>, out: &mut dyn Write) -> Result<(), String> {
    if args.first().is_some_and(|a| a == "--help" || a == "-h")
        || args.first().is_some_and(|a| a == "report")
            && args.get(1).is_some_and(|a| a == "--help" || a == "-h")
    {
        return writeln!(out, "{HELP}").map_err(|e| e.to_string());
    }
    let report = args.first().is_some_and(|a| a == "report");
    let mut args = args.into_iter().skip(usize::from(report));
    let mut flags = BTreeMap::new();
    while let Some(flag) = args.next() {
        if flags.contains_key(&flag) {
            return Err("duplicate health flag".into());
        }
        let value = match flag.as_str() {
            "--json" => String::new(),
            "--baseline" if report => String::new(),
            "--state" | "--since" | "--until" if report || flag == "--state" => args
                .next()
                .filter(|a| !a.starts_with("--"))
                .ok_or("missing health flag value")?,
            _ => return Err("unknown health flag".into()),
        };
        flags.insert(flag, value);
    }
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_err(|e| e.to_string())?
        .as_secs() as i64;
    let (since, until) = if report {
        (
            timestamp(flags.get("--since").ok_or("missing --since")?)?,
            timestamp(flags.get("--until").ok_or("missing --until")?)?,
        )
    } else {
        if !flags.contains_key("--json") {
            return Err("missing --json".into());
        }
        (i64::MIN, now.saturating_add(1))
    };
    let state = crate::state::resolve_state(flags.get("--state").map(String::as_str), |k| {
        std::env::var_os(k)
    })?;
    let evidence = crate::health_report_rows::read_rows(&state).map_err(|e| e.to_string())?;
    let result = crate::health_report::build_report(
        &crate::records::list_runs(&state),
        &evidence,
        since,
        until,
    )
    .map_err(|e| e.to_string())?;
    if flags.contains_key("--baseline") {
        baseline(&state, since, until, &result)?;
    }
    writeln!(out, "{}", result).map_err(|e| e.to_string())?;
    if report && !flags.contains_key("--json") {
        writeln!(
            out,
            "Health research: {} launches; {} unusable run-hours",
            result["totals"]["launches"], result["totals"]["unusable_run_hours"]
        )
        .map_err(|e| e.to_string())?;
    }
    Ok(())
}
fn baseline(state: &std::path::Path, since: i64, until: i64, report: &Value) -> Result<(), String> {
    let dir = state.join("reports");
    let path = dir.join(format!("baseline-{since}-{until}.json"));
    crate::health_report_rows::regular(state, true)
        .and_then(|_| crate::health_report_rows::regular(&dir, true))
        .and_then(|_| crate::health_report_rows::regular(&path, false))
        .map_err(|e| e.to_string())?;
    pg_worker_core::run::record_io::atomic_write_private(&path, report.to_string().as_bytes())
        .map_err(|e| e.to_string())
}
fn timestamp(s: &str) -> Result<i64, String> {
    if s.as_bytes().get(10) != Some(&b'T') {
        return Err("invalid RFC3339 timestamp".into());
    }
    pg_worker_core::reset::http_date(s)
        .map(|t| t.floor() as i64)
        .ok_or("invalid RFC3339 timestamp".into())
}

#[cfg(test)]
#[path = "health_cli_tests.rs"]
mod tests;
