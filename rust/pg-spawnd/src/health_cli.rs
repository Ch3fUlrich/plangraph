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
    if since >= until {
        return Err(crate::health_report::ReportError::InvalidWindow.to_string());
    }
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
pub(crate) fn timestamp(s: &str) -> Result<i64, String> {
    let parse = || -> Option<i64> {
        let b = s.as_bytes();
        if !s.is_ascii()
            || !matches!(b.len(), 20 | 25)
            || b[4] != b'-'
            || b[7] != b'-'
            || b[10] != b'T'
            || b[13] != b':'
            || b[16] != b':'
        {
            return None;
        }
        let number = |a: usize, z: usize| -> Option<i64> {
            b[a..z].iter().all(u8::is_ascii_digit).then_some(())?;
            s[a..z].parse().ok()
        };
        let (year, month, day) = (number(0, 4)?, number(5, 7)?, number(8, 10)?);
        let leap = year % 4 == 0 && (year % 100 != 0 || year % 400 == 0);
        let days = match month {
            2 => 28 + i64::from(leap),
            4 | 6 | 9 | 11 => 30,
            1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
            _ => return None,
        };
        let (hour, minute, second) = (number(11, 13)?, number(14, 16)?, number(17, 19)?);
        if day < 1 || day > days || hour > 23 || minute > 59 || second > 59 {
            return None;
        }
        let offset = if b.len() == 20 {
            if b[19] != b'Z' {
                return None;
            }
            0
        } else {
            if !matches!(b[19], b'+' | b'-') || b[22] != b':' {
                return None;
            }
            let (h, m) = (number(20, 22)?, number(23, 25)?);
            if h > 23 || m > 59 {
                return None;
            }
            (h * 3600 + m * 60) * if b[19] == b'+' { 1 } else { -1 }
        };
        let y = year - i64::from(month <= 2);
        let era = y.div_euclid(400);
        let yo = y - era * 400;
        let m = month + if month > 2 { -3 } else { 9 };
        let days =
            era * 146097 + yo * 365 + yo / 4 - yo / 100 + (153 * m + 2) / 5 + day - 1 - 719468;
        Some(days * 86400 + hour * 3600 + minute * 60 + second - offset)
    };
    parse().ok_or_else(|| "invalid RFC3339 timestamp".into())
}

#[cfg(test)]
#[path = "health_cli_tests.rs"]
mod tests;
