//! The GO check of the deploy manifest: a train is refused while a manifest entry has been not live for
//! more than 24 hours on any host that reports, unless a dated routing exception covers it.
//!
//! The ledgers are the files `deploy-status` writes, one per host. Exceptions are `deploy/exceptions.toml`,
//! compiled into the verifier like the other pins, so a change under test cannot grant itself one:
//! `[[exception]] entry = "unit:<name>" | "binary:<package>/<bin>", until = "YYYY-MM-DD", decision = "D-<n>",
//! reason = "..."`. An exception ends at the end of its `until` day (UTC).
use super::ledger::{self, Ledger};
use serde::Deserialize;
use std::path::Path;

/// How long an entry may stay not live before the GO check refuses.
pub const LIMIT_HOURS: i64 = 24;
/// A ledger older than this is not evidence of anything: its host stopped running deploy-status.
pub const LEDGER_MAX_AGE_HOURS: i64 = 6;

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Exception {
    pub entry: String,
    pub until: String,
    pub decision: String,
    pub reason: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Exceptions {
    #[serde(default)]
    exception: Vec<Exception>,
}

/// Parse and validate the exception file text.
pub fn parse_exceptions(text: &str) -> Result<Vec<Exception>, String> {
    let parsed: Exceptions = toml::from_str(text).map_err(|e| format!("exceptions: {e}"))?;
    for e in &parsed.exception {
        let d = e.decision.strip_prefix("D-").unwrap_or("");
        if d.is_empty() || !d.bytes().all(|b| b.is_ascii_digit()) {
            return Err(format!(
                "exception {}: decision {:?} is not a D-id",
                e.entry, e.decision
            ));
        }
        if until_epoch(&e.until).is_none() {
            return Err(format!(
                "exception {}: until {:?} is not a YYYY-MM-DD date",
                e.entry, e.until
            ));
        }
        if e.reason.trim().is_empty() {
            return Err(format!("exception {}: a reason is required", e.entry));
        }
    }
    Ok(parsed.exception)
}

/// The first second after the `until` day, UTC.
fn until_epoch(until: &str) -> Option<i64> {
    let day = chrono::NaiveDate::parse_from_str(until, "%Y-%m-%d").ok()?;
    Some(day.succ_opt()?.and_hms_opt(0, 0, 0)?.and_utc().timestamp())
}

/// One reason the GO check refuses: a stable code and the detail naming host, entry, state and age.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Finding {
    pub code: &'static str,
    pub detail: String,
}

/// Read every `*.json` ledger of a directory as (host, ledger); an unreadable file is a finding.
pub fn read_ledgers(dir: &Path) -> (Vec<(String, Ledger)>, Vec<Finding>) {
    let (mut ledgers, mut findings) = (Vec::new(), Vec::new());
    let Ok(read) = std::fs::read_dir(dir) else {
        return (ledgers, findings);
    };
    let mut paths: Vec<_> = read.filter_map(Result::ok).map(|e| e.path()).collect();
    paths.sort();
    for path in paths
        .iter()
        .filter(|p| p.extension().is_some_and(|e| e == "json"))
    {
        let host = path
            .file_stem()
            .map(|s| s.to_string_lossy().into_owned())
            .unwrap_or_default();
        match std::fs::read(path)
            .ok()
            .and_then(|b| serde_json::from_slice::<Ledger>(&b).ok())
        {
            Some(ledger) => ledgers.push((host, ledger)),
            None => findings.push(Finding {
                code: "deploy_ledger_unreadable",
                detail: format!("{}: not a deploy-status ledger", path.display()),
            }),
        }
    }
    (ledgers, findings)
}

/// Every refusal of the deploy part of the GO check at `now`.
pub fn check(ledgers: &[(String, Ledger)], exceptions: &[Exception], now: i64) -> Vec<Finding> {
    if ledgers.is_empty() {
        return vec![Finding {
            code: "deploy_ledger_missing",
            detail: "no host has written a deploy-status ledger; run `plangraph deploy-status` on every host".into(),
        }];
    }
    let mut out = Vec::new();
    for (host, ledger) in ledgers {
        let stale = ledger::age_hours(ledger.written_at, now);
        if stale > LEDGER_MAX_AGE_HOURS {
            out.push(Finding {
                code: "deploy_ledger_stale",
                detail: format!(
                    "{host}: deploy-status last ran {stale}h ago (limit {LEDGER_MAX_AGE_HOURS}h)"
                ),
            });
        }
        for (key, since) in &ledger.not_live_since {
            let age = ledger::age_hours(*since, now);
            if age <= LIMIT_HOURS {
                continue;
            }
            let excused = exceptions
                .iter()
                .any(|e| e.entry == *key && until_epoch(&e.until).is_some_and(|end| now < end));
            if !excused {
                let state = ledger.state.get(key).map_or("not live", String::as_str);
                out.push(Finding {
                    code: "deploy_not_live",
                    detail: format!("{host}: {key} is {state} for {age}h (limit {LIMIT_HOURS}h); install it or add a dated routing exception"),
                });
            }
        }
    }
    out
}
