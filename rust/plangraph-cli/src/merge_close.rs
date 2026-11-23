//! `plangraph merge-close`: the keeper closes every item a merge into main names.
//!
//! The timer refreshes the merge index, then closes each open,
//! claimed or in-review item named by a `Plan-Item` trailer: no router, no decision id.
//! The job prints one line per closed item and one line per key the keeper refused or
//! flagged, with the reason, so routing sees every merged key that stayed open.
//! A timer runs it every ten minutes, and the batch land step calls [`run_once`].
use crate::feeder::{Env, SlowKeeper, Transport, MERGE_INDEX_EXPIRED, MERGE_INDEX_RUNNING};
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::fmt::Write as _;
use std::time::{Duration, Instant};

/// Keep a bound on each keeper call, including a page of close results.
const MAX_TIME_SECS: u32 = 300;
const REFRESH_TIMEOUT: Duration = Duration::from_secs(95);
// Leave time for a final control reply and logging before the service's 420 s timeout.
const RECOVERY_BUDGET: Duration = Duration::from_secs(400);
const SWITCH: &str = "PG_MERGE_CLOSE_REQUIRE_SEATS";

/// What one run did, as the keeper reported it.
#[derive(Debug, PartialEq)]
pub struct Outcome {
    pub text: String,
    pub merged_not_done: u64,
}

/// The `require_seats` switch: unset leaves the keeper's own `require_seats` setting in force
/// (the request omits the field); `1`, `true` or `on` demands seats, `0`, `false` or `off` flags a
/// missing seat instead.
pub fn require_seats(env: &Env) -> Result<Option<bool>, String> {
    match env
        .get(SWITCH)
        .map(|v| v.trim().to_ascii_lowercase())
        .as_deref()
    {
        None | Some("") => Ok(None),
        Some("1" | "true" | "on") => Ok(Some(true)),
        Some("0" | "false" | "off") => Ok(Some(false)),
        Some(_) => Err(format!("{SWITCH} must be 1/true/on or 0/false/off")),
    }
}

/// Close what is merged (`apply`) or only count it, through any keeper transport.
pub fn run_with(
    keeper: &dyn Transport,
    require_seats: Option<bool>,
    apply: bool,
    project: Option<&str>,
) -> Result<Outcome, String> {
    let mut pass = ClosePass::default();
    pass.collect(require_seats, apply, project, |body| {
        keeper.call("close_merged_auto", Some(body))
    })?;
    Ok(pass.finish(false))
}

#[derive(Default)]
struct ClosePass {
    text: String,
    merged_not_done: u64,
    closed_any: bool,
    scan_errors: BTreeMap<String, String>,
    deployment_text: String,
    deployment_count: Option<usize>,
}

impl ClosePass {
    fn collect(
        &mut self,
        require_seats: Option<bool>,
        apply: bool,
        project: Option<&str>,
        mut page: impl FnMut(Value) -> Result<Value, String>,
    ) -> Result<(), String> {
        let mut body = json!({"apply": apply});
        if let Some(require_seats) = require_seats {
            body["require_seats"] = json!(require_seats);
        }
        if let Some(project) = project {
            body["project"] = json!(project);
        }
        loop {
            let result = page(body.clone())?;
            self.merged_not_done += result["merged_not_done"]
                .as_u64()
                .ok_or("keeper reply has no merged_not_done count")?;
            if let Some(errors) = result["scan_errors"].as_object() {
                for (repo, reason) in errors {
                    let reason = reason.as_str().ok_or("keeper scan error is not text")?;
                    self.scan_errors
                        .insert(repo.clone(), reason.replace(['\n', '\t'], " "));
                }
            }
            page_text(&mut self.text, &result);
            self.closed_any |= result["closed"]
                .as_array()
                .is_some_and(|rows| !rows.is_empty());
            if let Some(deployments) = result["deployment_pending"].as_array() {
                *self.deployment_count.get_or_insert(0) += deployments.len();
                for row in deployments {
                    deployment_text(&mut self.deployment_text, row);
                }
            }
            let Some(after) = result["next_after"].as_str() else {
                return if result["next_after"].is_null() {
                    Ok(())
                } else {
                    Err("keeper continuation is not an item key".into())
                };
            };
            if after.is_empty() || body["after"].as_str().is_some_and(|prior| after <= prior) {
                return Err("keeper continuation did not advance".into());
            }
            body["after"] = json!(after);
        }
    }

    fn finish(mut self, deployment_only: bool) -> Outcome {
        if deployment_only {
            self.text.clear();
        }
        self.text.push_str(&self.deployment_text);
        if let Some(count) = self.deployment_count {
            let _ = writeln!(self.text, "merged-not-live\t{count}");
        }
        for (repo, reason) in self.scan_errors {
            let _ = writeln!(self.text, "scan-error\t{repo}\t{reason}");
        }
        let merged_not_done = self.merged_not_done;
        if !deployment_only {
            let _ = writeln!(self.text, "merged-not-done\t{merged_not_done}");
        }
        Outcome {
            text: self.text,
            merged_not_done,
        }
    }
}

fn deployment_text(text: &mut String, row: &Value) {
    let age = row["age_hours"]
        .as_u64()
        .map_or_else(|| "?".into(), |n| format!("{n}h"));
    let missing = row["missing"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .collect::<Vec<_>>()
        .join(",");
    let _ = writeln!(
        text,
        "deployment-pending\t{}\t{age}\t{missing}",
        row["key"].as_str().unwrap_or("?")
    );
}

fn page_text(text: &mut String, result: &Value) {
    let rows = |name: &str| result[name].as_array().into_iter().flatten();
    for (label, name) in [("closed", "closed"), ("would-close", "would_close")] {
        for row in rows(name) {
            let heads: Vec<&str> = row["prs"]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(|p| p["head"].as_str())
                .collect();
            let _ = writeln!(
                text,
                "{label}\t{}\t{}{}",
                row["key"].as_str().unwrap_or("?"),
                heads.join(","),
                flagged(&row["flags"])
            );
        }
    }
    for row in rows("parts") {
        let _ = writeln!(
            text,
            "merged-part\t{}\t{}\t{}",
            row["key"].as_str().unwrap_or("?"),
            row["pr"],
            row["merge_sha"].as_str().unwrap_or("?")
        );
    }
    for row in rows("listed") {
        let _ = writeln!(
            text,
            "listed\t{}\t{}\t{}",
            row["key"].as_str().unwrap_or("?"),
            row["state"].as_str().unwrap_or("?"),
            row["reason"]
                .as_str()
                .unwrap_or("?")
                .replace(['\n', '\t'], " ")
        );
    }
}

/// `\tFLAGGED name: run ids; ...` for the flags a close stored (`seats_missing`), else nothing.
fn flagged(flags: &Value) -> String {
    let Some(flags) = flags.as_object().filter(|f| !f.is_empty()) else {
        return String::new();
    };
    let parts: Vec<String> = flags
        .iter()
        .map(|(name, flag)| {
            let runs: Vec<&str> = flag["run_ids"]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(Value::as_str)
                .collect();
            if runs.is_empty() {
                name.clone()
            } else {
                format!("{name}: {}", runs.join(","))
            }
        })
        .collect();
    format!("\tFLAGGED {}", parts.join("; "))
}

/// The repositories the keeper's merged-on-default scan reads (`repos`, or `merge_scan` of its config).
pub fn repos_with(keeper: &dyn Transport) -> Result<Vec<String>, String> {
    let result = keeper.call("close_merged_auto", Some(json!({"repos_only": true})))?;
    result["repos"]
        .as_array()
        .ok_or("keeper reply has no repos list")?
        .iter()
        .map(|name| {
            name.as_str()
                .map(str::to_owned)
                .ok_or("keeper repo name is not text".to_owned())
        })
        .collect()
}

fn run_scheduled(
    env: &Env,
    apply: bool,
    project: Option<&str>,
    deployment_only: bool,
) -> Result<Outcome, String> {
    let seats = require_seats(env)?;
    let keeper = SlowKeeper {
        env,
        max_time_secs: MAX_TIME_SECS,
    };
    let control = SlowKeeper {
        env,
        max_time_secs: 10,
    };
    run_scheduled_with(
        &keeper,
        &control,
        seats,
        apply,
        project,
        deployment_only,
        REFRESH_TIMEOUT,
    )
}

/// Run the scheduled refresh and paged close using the supplied keeper transports.
pub fn run_scheduled_with(
    keeper: &dyn Transport,
    control: &dyn Transport,
    seats: Option<bool>,
    apply: bool,
    project: Option<&str>,
    deployment_only: bool,
    refresh_timeout: Duration,
) -> Result<Outcome, String> {
    let deadline = Instant::now() + RECOVERY_BUDGET;
    refresh_index(control, refresh_timeout)?;
    let mut pass = ClosePass {
        deployment_count: Some(0),
        ..ClosePass::default()
    };
    let projects_reply;
    let projects = if let Some(project) = project {
        vec![project]
    } else {
        projects_reply = control.call("close_merged_auto", Some(json!({"refresh": "projects"})))?;
        projects_reply["projects"]
            .as_array()
            .ok_or("keeper reply has no project list")?
            .iter()
            .map(|project| project.as_str().ok_or("keeper project name is not text"))
            .collect::<Result<_, _>>()?
    };
    for (index, project) in projects.iter().enumerate() {
        let result = pass.collect(seats, apply, Some(project), |body| {
            let call = || keeper.call("close_merged_auto", Some(body.clone()));
            match call() {
                Err(error)
                    if apply && error == MERGE_INDEX_EXPIRED && Instant::now() < deadline =>
                {
                    let remaining = deadline.saturating_duration_since(Instant::now());
                    refresh_index(control, refresh_timeout.min(remaining))
                        .map_err(|reason| format!("{MERGE_INDEX_EXPIRED} ({reason})"))?;
                    if Instant::now() < deadline {
                        call()
                    } else {
                        Err(error)
                    }
                }
                result => result,
            }
        });
        if let Err(error) = result {
            if !apply
                || !pass.closed_any
                || !(error.starts_with(MERGE_INDEX_EXPIRED) || error == MERGE_INDEX_RUNNING)
            {
                return Err(error);
            }
            let remaining = projects.len() - index;
            let _ = writeln!(
                pass.text,
                "continue\t{remaining} projects remaining\t{error}"
            );
            break;
        }
    }
    if !apply && !pass.scan_errors.is_empty() {
        return Err(format!("incomplete merge scan: {:?}", pass.scan_errors));
    }
    Ok(pass.finish(deployment_only))
}

fn refresh_index(control: &dyn Transport, timeout: Duration) -> Result<(), String> {
    let deadline = Instant::now() + timeout;
    control.call("close_merged_auto", Some(json!({"refresh": "start"})))?;
    loop {
        let reply = control.call("close_merged_auto", Some(json!({"refresh": "status"})))?;
        match reply["scan"].as_str() {
            Some("ready") => return Ok(()),
            Some("running") => {
                let remaining = deadline.saturating_duration_since(Instant::now());
                if remaining.is_zero() {
                    return Err("merge index refresh is still running".into());
                }
                std::thread::sleep(remaining.min(Duration::from_secs(1)));
            }
            _ => return Err("keeper reply has no valid merge index status".into()),
        }
    }
}

/// The call the land step makes after a merge: the job with the configured switch.
pub fn run_once(env: &Env, project: Option<&str>) -> Result<Outcome, String> {
    run_scheduled(env, true, project, false)
}

/// `merge-close [--count | --repos | --deploy-status] [--project NAME]`.
/// The flags only report counts, repositories, or unproven deployments.
pub fn run(env: &Env, args: &[String]) -> i32 {
    let mut count = false;
    let mut repos = false;
    let mut deploy_status = false;
    let mut project = None;
    let mut rest = args.iter();
    while let Some(arg) = rest.next() {
        match arg.as_str() {
            "--count" => count = true,
            "--repos" => repos = true,
            "--deploy-status" => deploy_status = true,
            "--project" => match rest.next() {
                Some(name) => project = Some(name.as_str()),
                None => return usage(),
            },
            _ => return usage(),
        }
    }
    if u8::from(count) + u8::from(repos) + u8::from(deploy_status) > 1 {
        return usage();
    }
    if repos {
        let keeper = SlowKeeper {
            env,
            max_time_secs: MAX_TIME_SECS,
        };
        return match repos_with(&keeper) {
            Ok(names) => {
                names.iter().for_each(|name| println!("{name}"));
                0
            }
            Err(reason) => {
                eprintln!("merge-close: {reason}");
                1
            }
        };
    }
    let outcome = run_scheduled(env, !(count || deploy_status), project, deploy_status);
    match outcome {
        Ok(done) => {
            if count {
                println!("{}", done.merged_not_done);
            } else {
                print!("{}", done.text);
            }
            0
        }
        Err(reason) => {
            eprintln!("merge-close: {reason}");
            1
        }
    }
}

fn usage() -> i32 {
    eprintln!(
        "usage: plangraph merge-close [--count | --repos | --deploy-status] [--project NAME]"
    );
    2
}

/// Persist one routing proposal batch from the keeper's watched dry run.
pub fn reconcile_with(
    keeper: &dyn Transport,
    control: &dyn Transport,
    seats: Option<bool>,
    state: &std::path::Path,
    refresh_timeout: Duration,
) -> Result<String, String> {
    refresh_index(control, refresh_timeout)?;
    let mut request = json!({"apply": false, "watch": true});
    if let Some(required) = seats {
        request["require_seats"] = json!(required);
    }
    let report = keeper.call("close_merged_auto", Some(request))?;
    let count = report["merged_not_done"]
        .as_u64()
        .ok_or("missing merged count")?;
    let alert = report["alert"]
        .as_bool()
        .ok_or("missing watcher alert state")?;
    let close = report["would_close"]
        .as_array()
        .ok_or("missing close proposals")?;
    let listed = report["listed"]
        .as_array()
        .ok_or("missing refused proposals")?;
    if count != (close.len() + listed.len()) as u64 {
        return Err("merged count does not match the proposal rows".into());
    }
    let mut proposals = Vec::new();
    let mut refusals = std::collections::BTreeMap::new();
    for row in close {
        proposals.push(json!({"action": "CLOSE", "item": row}));
    }
    for row in listed {
        let reason = row["reason"].as_str().ok_or("missing refusal reason")?;
        let (code, detail) = reason.split_once(": ").unwrap_or((reason, ""));
        let tag = detail.split(':').next().unwrap_or_default();
        let tag = match code {
            "coverage" | "same_family" | "family_unknown" => "seats_missing",
            "evidence_unverified" if detail.starts_with("NO-WITNESS ") => "seats_missing",
            _ if !tag.is_empty() && tag.bytes().all(|b| b.is_ascii_lowercase() || b == b'_') => tag,
            _ => code,
        };
        *refusals.entry(tag).or_insert(0usize) += 1;
        let action = match tag {
            "decision_merge_only" => "CLOSE",
            "unmerged_work_or_lanes_unknown" => "SLICE-OPEN",
            "deploy_pending" | "seats_missing" => continue,
            _ => "RESCOPE",
        };
        proposals.push(json!({"action": action, "item": row}));
    }
    let batch = json!({"merged_not_done": count, "alert": alert, "refusals": refusals,
                       "proposals": proposals});
    let text = batch.to_string();
    pg_worker_core::run::record_io::atomic_write_private(state, text.as_bytes())
        .map_err(|err| format!("write watcher state {}: {err}", state.display()))?;
    Ok(format!("reconcile-merged\t{text}"))
}

pub fn reconcile(env: &Env, args: &[String]) -> i32 {
    let [dry_run, flag, state] = args else {
        eprintln!("usage: plangraph reconcile-merged --dry-run --state PATH");
        return 2;
    };
    if dry_run != "--dry-run" || flag != "--state" {
        eprintln!("usage: plangraph reconcile-merged --dry-run --state PATH");
        return 2;
    }
    let keeper = SlowKeeper {
        env,
        max_time_secs: MAX_TIME_SECS,
    };
    let control = SlowKeeper {
        env,
        max_time_secs: 10,
    };
    match require_seats(env)
        .and_then(|seats| reconcile_with(&keeper, &control, seats, state.as_ref(), REFRESH_TIMEOUT))
    {
        Ok(line) => {
            println!("{line}");
            0
        }
        Err(error) => {
            eprintln!("reconcile-merged: {error}");
            1
        }
    }
}
