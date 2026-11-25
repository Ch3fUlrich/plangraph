//! `plangraph deploy-evidence`: merged work that is not running on a host, and the router GO gate input.
//!
//! `report` joins the keeper's `deployment_pending` rows (open items, and completed items when the
//! keeper scans them) with the live state of each named unit or binary on its host. `gate` reads
//! that report: a pending deployment older than [`MAX_AGE_HOURS`] blocks GO unless a dated routing
//! exception names its key. The router calls `gate --report FILE --exceptions FILE`: exit 0 is GO,
//! exit 1 is blocked, exit 2 is an unreadable input.
use crate::feeder::Env;
use chrono::{DateTime, NaiveDate};
use serde_json::{json, Value};
use std::process::Command;

/// A pending deployment younger than this does not hold a GO.
pub const MAX_AGE_HOURS: u64 = 24;
/// The longest a routing exception may run.
pub const MAX_EXCEPTION_DAYS: i64 = 14;
/// A saved report older than this (seconds) is refused: its ages and host states are stale.
pub const MAX_REPORT_AGE_SECS: i64 = 3600;
/// How far a timestamp may lie ahead of the gate's clock before it is not believed.
pub const MAX_CLOCK_SKEW_SECS: i64 = 300;

/// What a host says about one deploy target.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Probed {
    /// A unit answers `active` (or another systemd state); a `bin:NAME` target answers `installed` or `missing`.
    pub state: String,
    /// The commit the installed build was made from, when the host can name it.
    pub revision: Option<String>,
}

/// Live state of one deploy target on one host.
pub trait HostProbe {
    fn state(&self, host: &str, target: &str) -> Result<Probed, String>;
    /// True when `revision` contains every one of the `landed` commits.
    fn reached(&self, landed: &[String], revision: &str) -> bool;
}

/// Runs `PROGRAM HOST TARGET`; its first word is the state and an optional second word the revision.
/// `systemctl is-active` exits non-zero for a unit that is not active but still prints its state, so
/// the exit status is not read. `repo` is the checkout whose history decides whether a revision
/// contains a landing; without it no revision is believed.
pub struct CommandProbe {
    pub program: String,
    pub repo: Option<String>,
}

impl HostProbe for CommandProbe {
    fn state(&self, host: &str, target: &str) -> Result<Probed, String> {
        let out = Command::new(&self.program)
            .arg(host)
            .arg(target)
            .output()
            .map_err(|e| format!("probe did not start: {e}"))?;
        let text = String::from_utf8_lossy(&out.stdout);
        let mut words = text.split_whitespace();
        let state = words
            .next()
            .ok_or_else(|| format!("probe printed no state (exit {})", out.status))?;
        Ok(Probed {
            state: state.to_string(),
            revision: words.next().map(str::to_string),
        })
    }

    fn reached(&self, landed: &[String], revision: &str) -> bool {
        let Some(repo) = &self.repo else { return false };
        let sha = |text: &str| text.len() >= 7 && text.bytes().all(|b| b.is_ascii_hexdigit());
        !landed.is_empty()
            && sha(revision)
            && landed.iter().all(|commit| {
                sha(commit)
                    && Command::new("git")
                        .args(["-C", repo, "merge-base", "--is-ancestor", commit, revision])
                        .output()
                        .is_ok_and(|out| out.status.success())
            })
    }
}

/// The prefix of a `missing` entry that stands for an item the keeper could not scan.
pub const SCAN_UNVERIFIED: &str = "scan_unverified";

/// One expected `host:unit` target. `receipted` says the keeper holds deploy evidence for it; only a
/// receipt plus a host that runs a build containing every landing clears it.
fn target_row(entry: &str, probe: &dyn HostProbe, landed: &[String], receipted: bool) -> Value {
    let reason = |status: &str| json!({"target": entry, "status": status, "blocking": true});
    if entry.starts_with(SCAN_UNVERIFIED) {
        return reason("scan_unverified");
    }
    match entry {
        "(hosts unnamed)" => return reason("hosts_unnamed"),
        "live_evidence_missing" => return reason("live_proof_missing"),
        _ => {}
    }
    let Some((host, unit)) = entry.split_once(':') else {
        return reason("unreadable_target");
    };
    let (live, dead) = if unit.starts_with("bin:") {
        ("installed", "binary_missing")
    } else {
        ("active", "unit_inactive")
    };
    let (seen, revision, status) = match probe.state(host, unit) {
        Ok(p) if p.state == live && !receipted => (p.state, p.revision, "unrecorded"),
        Ok(p) if p.state == live => {
            let proven = p
                .revision
                .as_deref()
                .is_some_and(|r| probe.reached(landed, r));
            (
                p.state,
                p.revision,
                if proven {
                    "verified"
                } else {
                    "revision_unproven"
                },
            )
        }
        Ok(p) => (p.state, p.revision, dead),
        Err(why) => (why, None, "host_unverified"),
    };
    json!({"target": entry, "host": host, "unit": unit, "probe": seen, "revision": revision,
           "status": status, "blocking": status != "verified"})
}

/// The report of the keeper's rows with the live host state of every expected target.
pub fn build_report(rows: &[Value], probe: &dyn HostProbe, now: i64) -> Value {
    let texts = |row: &Value, field: &str| -> Vec<String> {
        row[field]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
            .map(str::to_string)
            .collect()
    };
    let rows: Vec<Value> = rows
        .iter()
        .map(|row| {
            let (missing, landed) = (texts(row, "missing"), texts(row, "landed"));
            let mut targets: Vec<Value> = missing
                .iter()
                .map(|entry| target_row(entry, probe, &landed, false))
                .collect();
            // Receipted targets are probed too: a receipt alone is only the router's word.
            targets.extend(
                texts(row, "expected")
                    .iter()
                    .filter(|entry| !missing.contains(entry))
                    .map(|entry| target_row(entry, probe, &landed, true)),
            );
            if targets.is_empty() {
                targets.push(target_row(SCAN_UNVERIFIED, probe, &landed, false));
            }
            let pending = targets.iter().any(|t| t["blocking"] == true);
            json!({"key": row["key"], "state": row["state"], "merged_at": row["merged_at"],
                   "age_hours": row["age_hours"], "landed": landed, "targets": targets,
                   "status": if pending { "pending" } else { "verified" }})
        })
        .collect();
    let pending = rows.iter().filter(|r| r["status"] == "pending").count();
    json!({"schema": "plangraph.deploy-evidence/1", "generated_at": now, "pending": pending, "rows": rows})
}

fn day(text: &Value) -> Option<NaiveDate> {
    NaiveDate::parse_from_str(text.as_str()?, "%Y-%m-%d").ok()
}

/// Why a routing exception does not count, or `None` when it covers its key today.
fn exception_defect(exception: &Value, today: NaiveDate) -> Option<String> {
    let id = exception["decision"].as_str().unwrap_or("");
    let numbered = id
        .strip_prefix("D-")
        .is_some_and(|n| !n.is_empty() && n.bytes().all(|b| b.is_ascii_digit()));
    let reason = exception["reason"]
        .as_str()
        .is_some_and(|r| !r.trim().is_empty());
    let (Some(granted), Some(expires)) = (day(&exception["granted"]), day(&exception["expires"]))
    else {
        return Some("granted and expires must be YYYY-MM-DD dates".into());
    };
    Some(match () {
        _ if !numbered => "decision must be a D-id".into(),
        _ if !reason => "reason is empty".into(),
        _ if (expires - granted).num_days() > MAX_EXCEPTION_DAYS => {
            format!("exception runs longer than {MAX_EXCEPTION_DAYS} days")
        }
        _ if granted > today => "exception is not granted yet".into(),
        _ if expires < today => format!("exception expired on {expires}"),
        _ => return None,
    })
}

/// Hours since the row's merge, read at gate time; unknown (and so blocking) when the merge time is
/// missing or unreadable. The age the keeper printed is never trusted: a saved report only gets older.
fn age_hours(row: &Value, now: i64) -> Option<u64> {
    let merged = DateTime::parse_from_rfc3339(row["merged_at"].as_str()?)
        .ok()?
        .timestamp();
    // A merge time ahead of the clock beyond the skew is wrong data, not a brand-new merge.
    let age = now.checked_sub(merged)?;
    (age >= -MAX_CLOCK_SKEW_SECS).then(|| age.max(0) as u64 / 3600)
}

/// The GO decision for a report: pending rows older than `max_age_hours` (or of unknown age) block,
/// unless a valid dated exception names the key.
pub fn gate(
    report: &Value,
    exceptions: &Value,
    now: i64,
    max_age_hours: u64,
) -> Result<Value, String> {
    validate_report(report, now)?;
    if !exceptions.is_array() {
        return Err("exceptions file is not a list".into());
    }
    let today = DateTime::from_timestamp(now, 0).map(|t| t.date_naive());
    let (mut blockers, mut excepted, mut waiting, mut invalid) = (vec![], vec![], vec![], vec![]);
    let listed = exceptions.as_array().map_or(&[][..], Vec::as_slice);
    for row in report["rows"].as_array().into_iter().flatten() {
        if row["status"] != "pending" {
            continue;
        }
        let age = age_hours(row, now);
        let reasons: Vec<&Value> = row["targets"]
            .as_array()
            .into_iter()
            .flatten()
            .filter(|t| t["blocking"] == true)
            .map(|t| &t["target"])
            .collect();
        let entry =
            json!({"key": row["key"], "state": row["state"], "age_hours": age, "missing": reasons});
        if age.is_some_and(|hours| hours < max_age_hours) {
            waiting.push(entry);
            continue;
        }
        let covering = listed.iter().find(|e| e["key"] == row["key"]);
        match (covering, today) {
            (Some(exception), Some(today)) => match exception_defect(exception, today) {
                None => excepted.push(json!({"key": row["key"], "decision": exception["decision"],
                    "expires": exception["expires"], "age_hours": age, "missing": reasons})),
                Some(why) => {
                    invalid.push(json!({"key": row["key"], "why": why}));
                    blockers.push(entry);
                }
            },
            _ => blockers.push(entry),
        }
    }
    Ok(
        json!({"go": blockers.is_empty(), "max_age_hours": max_age_hours, "blockers": blockers,
           "excepted": excepted, "waiting": waiting, "invalid_exceptions": invalid}),
    )
}

/// A report the gate may trust: the schema, a `rows` list and a `pending` count that agrees with it, and
/// for every row a key, the `pending` status, an age that is a number or null, and blocking targets.
pub fn validate_report(report: &Value, now: i64) -> Result<(), String> {
    if report["schema"] != "plangraph.deploy-evidence/1" {
        return Err("not a deploy-evidence report".into());
    }
    let made = report["generated_at"]
        .as_i64()
        .ok_or("report has no generated_at")?;
    // Checked arithmetic: a timestamp at the edge of the range is a refusal, never a panic.
    // Both bounds hang on one subtraction: an overflow in either direction is not fresh.
    let fresh = now
        .checked_sub(made)
        .is_some_and(|age| (-MAX_CLOCK_SKEW_SECS..=MAX_REPORT_AGE_SECS).contains(&age));
    if !fresh {
        return Err(format!(
            "report was generated at {made}, not within an hour of {now}"
        ));
    }
    let rows = report["rows"].as_array().ok_or("report has no rows list")?;
    let pending = rows.iter().filter(|r| r["status"] == "pending").count();
    if report["pending"].as_u64() != Some(pending as u64) {
        return Err(format!(
            "report pending count disagrees with its {pending} pending rows"
        ));
    }
    for (index, row) in rows.iter().enumerate() {
        let bad = |why: &str| Err(format!("row {index}: {why}"));
        if !row["key"].as_str().is_some_and(|k| !k.is_empty()) {
            return bad("no key");
        }
        let blocking = row["status"] == "pending";
        if !blocking && row["status"] != "verified" {
            return bad("status is neither pending nor verified");
        }
        if !(row["merged_at"].is_null() || row["merged_at"].is_string()) {
            return bad("merged_at is not text or null");
        }
        let targets = row["targets"].as_array().map_or(&[][..], Vec::as_slice);
        let any_blocking = targets.iter().any(|t| t["blocking"] == true);
        if targets.is_empty()
            || !targets
                .iter()
                .all(|t| t["target"].is_string() && t["blocking"].is_boolean())
            || any_blocking != blocking
        {
            return bad("targets are missing or disagree with the row status");
        }
    }
    Ok(())
}

const USAGE: &str = "usage: plangraph deploy-evidence report [--project NAME] [--no-done] [--probe PROGRAM] [--repo CHECKOUT] [--rows FILE] | gate --report FILE [--exceptions FILE] [--now EPOCH] [--max-age-hours N]";

fn flags(args: &[String], known: &[&str]) -> Result<Vec<(String, Option<String>)>, String> {
    let mut out = vec![];
    let mut rest = args.iter();
    while let Some(arg) = rest.next() {
        match arg.as_str() {
            "--no-done" if known.contains(&"--no-done") => out.push((arg.clone(), None)),
            name if known.contains(&name) => {
                let value = rest.next().ok_or(format!("{name} needs a value"))?;
                out.push((arg.clone(), Some(value.clone())));
            }
            other => return Err(format!("unknown argument {other}")),
        }
    }
    Ok(out)
}

fn flag<'a>(set: &'a [(String, Option<String>)], name: &str) -> Option<&'a str> {
    set.iter()
        .find(|(n, _)| n == name)
        .and_then(|(_, v)| v.as_deref())
}

fn read_json(path: &str) -> Result<Value, String> {
    let text = std::fs::read_to_string(path).map_err(|e| format!("{path}: {e}"))?;
    serde_json::from_str(&text).map_err(|e| format!("{path}: {e}"))
}

fn now() -> i64 {
    chrono::Utc::now().timestamp()
}

fn run_inner(env: &Env, args: &[String]) -> Result<i32, String> {
    let (verb, rest) = args.split_first().ok_or(USAGE)?;
    match verb.as_str() {
        "report" => {
            let set = flags(
                rest,
                &["--project", "--no-done", "--probe", "--repo", "--rows"],
            )?;
            let rows = match flag(&set, "--rows") {
                Some(path) => read_json(path)?
                    .as_array()
                    .cloned()
                    .ok_or("rows file is not a list")?,
                None => crate::merge_close::deployment_rows(
                    env,
                    flag(&set, "--project"),
                    !set.iter().any(|(n, _)| n == "--no-done"),
                )?,
            };
            let probe = CommandProbe {
                program: flag(&set, "--probe")
                    .ok_or("report needs --probe PROGRAM")?
                    .into(),
                repo: flag(&set, "--repo").map(str::to_string),
            };
            println!("{}", build_report(&rows, &probe, now()));
            Ok(0)
        }
        "gate" => {
            let set = flags(
                rest,
                &["--report", "--exceptions", "--now", "--max-age-hours"],
            )?;
            let report = read_json(flag(&set, "--report").ok_or("gate needs --report FILE")?)?;
            let exceptions = match flag(&set, "--exceptions") {
                Some(path) => read_json(path)?,
                None => json!([]),
            };
            let number = |name: &str, default: i64| match flag(&set, name) {
                Some(text) => text
                    .parse::<i64>()
                    .map_err(|_| format!("{name} is not a number")),
                None => Ok(default),
            };
            let hours = number("--max-age-hours", MAX_AGE_HOURS as i64)?;
            let decision = gate(
                &report,
                &exceptions,
                number("--now", now())?,
                hours.max(0) as u64,
            )?;
            println!("{decision}");
            Ok(if decision["go"] == true { 0 } else { 1 })
        }
        _ => Err(USAGE.into()),
    }
}

/// `deploy-evidence report|gate`; see the module comment.
pub fn run(env: &Env, args: &[String]) -> i32 {
    run_inner(env, args).unwrap_or_else(|reason| {
        eprintln!("deploy-evidence: {reason}");
        2
    })
}
