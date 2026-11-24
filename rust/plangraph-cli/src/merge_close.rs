//! `plangraph merge-close`: the keeper closes every item a merge into main names.
//!
//! The timer refreshes the merge index, then closes each open,
//! claimed or in-review item named by a `Plan-Item` trailer: no router, no decision id.
//! The job prints one line per closed item and one line per key the keeper refused or
//! flagged, with the reason, so routing sees every merged key that stayed open.
//! A timer runs it every ten minutes, and the batch land step calls [`run_once`].
use crate::feeder::{Env, SlowKeeper, Transport, MERGE_INDEX_EXPIRED};
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::fmt::Write as _;
use std::time::{Duration, Instant};

/// Keep a bound on each keeper call, including a page of close results.
const MAX_TIME_SECS: u32 = 300;
const REFRESH_TIMEOUT: Duration = Duration::from_secs(95);
/// The scheduled pass waits this long for the keeper scan (its budget is 180 s, see
/// `MergeRefresh.SCAN_BUDGET_SECONDS`), which leaves 150 s of the 300 s index age and 250 s of the
/// pass budget for paging. A scan still running then is continued by the next pass.
pub(crate) const SCHEDULED_REFRESH_WAIT: Duration = Duration::from_secs(150);
pub(crate) const WAIT_SWITCH: &str = "PG_MERGE_CLOSE_REFRESH_WAIT_SECS";
const REFRESH_STILL_RUNNING: &str = "merge index refresh is still running";
/// Consecutive scheduled passes that end on a pending refresh before the unit fails.
pub const PENDING_LIMIT: u32 = 3;
/// A pending pass is continued only while its scan can still be read inside the 300 s index age.
pub(crate) const CONTINUE_WINDOW_SECS: u64 = 240;
// Leave time for process overhead and logging before the service's 420 s timeout.
pub(crate) const PASS_BUDGET: Duration = Duration::from_secs(400);
const SWITCH: &str = "PG_MERGE_CLOSE_REQUIRE_SEATS";

/// What one run did, as the keeper reported it.
#[derive(Debug, PartialEq)]
pub struct Outcome {
    pub text: String,
    pub merged_not_done: u64,
}

/// The refresh limit and shared deadline for one scheduled pass.
pub struct ScheduleTiming {
    refresh_timeout: Duration,
    deadline: Instant,
    resume: bool,
}

impl ScheduleTiming {
    pub fn new(refresh_timeout: Duration, budget: Duration) -> Self {
        Self {
            refresh_timeout,
            deadline: Instant::now() + budget,
            resume: false,
        }
    }

    /// Continue a pending scan: reuse a fresh completed scan another caller already consumed.
    pub fn resuming(mut self) -> Self {
        self.resume = true;
        self
    }
}

/// How the keeper's index refresh ended inside its wait.
#[derive(Debug, PartialEq)]
pub enum Refresh {
    Ready,
    /// The keeper scan was still `running` when the wait ended.
    Pending {
        waited_secs: u64,
    },
}

/// What a scheduled pass ended with.
#[derive(Debug, PartialEq)]
pub enum Pass {
    Done(Outcome),
    Pending { waited_secs: u64 },
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

struct DeadlineKeeper<'a> {
    keeper: SlowKeeper<'a>,
    deadline: Instant,
}

impl Transport for DeadlineKeeper<'_> {
    fn call(&self, verb: &str, body: Option<Value>) -> Result<Value, String> {
        self.call_with(Instant::now(), |keeper| keeper.call(verb, body))
    }
}

impl DeadlineKeeper<'_> {
    fn call_with(
        &self,
        now: Instant,
        request: impl FnOnce(SlowKeeper<'_>) -> Result<Value, String>,
    ) -> Result<Value, String> {
        let remaining = self.deadline.saturating_duration_since(now);
        if remaining.is_zero() {
            return Err(MERGE_INDEX_EXPIRED.into());
        }
        // curl takes whole seconds here. Round up within the unit's shutdown margin.
        let remaining_secs = remaining.as_secs() + u64::from(remaining.subsec_nanos() > 0);
        let request_limit = u64::from(self.keeper.max_time_secs);
        let result = request(SlowKeeper {
            env: self.keeper.env,
            max_time_secs: request_limit.min(remaining_secs) as u32,
        });
        match result {
            Err(_) if remaining_secs <= request_limit && Instant::now() >= self.deadline => {
                Err(MERGE_INDEX_EXPIRED.into())
            }
            result => result,
        }
    }
}

fn run_scheduled(
    env: &Env,
    apply: bool,
    project: Option<&str>,
    deployment_only: bool,
) -> Result<Outcome, String> {
    let timing = ScheduleTiming::new(REFRESH_TIMEOUT, PASS_BUDGET);
    match scheduled_pass(
        env,
        apply,
        project,
        deployment_only,
        timing,
        &mut |_| Ok(()),
    )? {
        Pass::Done(outcome) => Ok(outcome),
        Pass::Pending { .. } => Err(REFRESH_STILL_RUNNING.into()),
    }
}

pub(crate) fn scheduled_pass(
    env: &Env,
    apply: bool,
    project: Option<&str>,
    deployment_only: bool,
    timing: ScheduleTiming,
    on_refresh: &mut dyn FnMut(&Refresh) -> Result<(), String>,
) -> Result<Pass, String> {
    let seats = require_seats(env)?;
    let deadline = timing.deadline;
    let transport = |max_time_secs| DeadlineKeeper {
        keeper: SlowKeeper { env, max_time_secs },
        deadline,
    };
    run_pass(
        &transport(MAX_TIME_SECS),
        &transport(10),
        seats,
        apply,
        project,
        deployment_only,
        timing,
        on_refresh,
    )
}

/// Run the scheduled refresh and paged close using the supplied keeper transports; a refresh
/// still running at its wait is the error `merge index refresh is still running`.
pub fn run_scheduled_with(
    keeper: &dyn Transport,
    control: &dyn Transport,
    seats: Option<bool>,
    apply: bool,
    project: Option<&str>,
    deployment_only: bool,
    timing: ScheduleTiming,
) -> Result<Outcome, String> {
    match run_pass(
        keeper,
        control,
        seats,
        apply,
        project,
        deployment_only,
        timing,
        &mut |_| Ok(()),
    )? {
        Pass::Done(outcome) => Ok(outcome),
        Pass::Pending { .. } => Err(REFRESH_STILL_RUNNING.into()),
    }
}

/// The scheduled pass. `on_refresh` sees the first refresh outcome before any page is read, so
/// the caller commits its pending streak before a close can be interrupted.
#[allow(clippy::too_many_arguments)]
pub fn run_pass(
    keeper: &dyn Transport,
    control: &dyn Transport,
    seats: Option<bool>,
    apply: bool,
    project: Option<&str>,
    deployment_only: bool,
    timing: ScheduleTiming,
    on_refresh: &mut dyn FnMut(&Refresh) -> Result<(), String>,
) -> Result<Pass, String> {
    let ScheduleTiming {
        refresh_timeout,
        deadline,
        resume,
    } = timing;
    let first = refresh_index(control, refresh_timeout, resume)?;
    on_refresh(&first)?;
    if let Refresh::Pending { waited_secs } = first {
        return Ok(Pass::Pending { waited_secs });
    }
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
            let call = || {
                if Instant::now() >= deadline {
                    return Err(MERGE_INDEX_EXPIRED.into());
                }
                keeper.call("close_merged_auto", Some(body.clone()))
            };
            match call() {
                Err(error)
                    if apply && error == MERGE_INDEX_EXPIRED && Instant::now() < deadline =>
                {
                    let remaining = deadline.saturating_duration_since(Instant::now());
                    // Only the first refresh of a pass counts toward the pending streak: a
                    // refresh that is still running here keeps the expiry the pass reports.
                    match refresh_index(control, refresh_timeout.min(remaining), false)? {
                        Refresh::Ready => call(),
                        Refresh::Pending { .. } => Err(error),
                    }
                }
                result => result,
            }
        });
        if let Err(error) = result {
            if !apply || !pass.closed_any || error != MERGE_INDEX_EXPIRED {
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
    Ok(Pass::Done(pass.finish(deployment_only)))
}

/// Start the keeper scan (`resume` continues a fresh completed one) and wait for it. A scan still
/// `running` at the wait is [`Refresh::Pending`]; every other failure stays an error.
fn refresh_index(
    control: &dyn Transport,
    timeout: Duration,
    resume: bool,
) -> Result<Refresh, String> {
    let begun = Instant::now();
    let deadline = begun + timeout;
    let verb = if resume { "resume" } else { "start" };
    control.call("close_merged_auto", Some(json!({"refresh": verb})))?;
    loop {
        let reply = control.call("close_merged_auto", Some(json!({"refresh": "status"})))?;
        match reply["scan"].as_str() {
            Some("ready") => return Ok(Refresh::Ready),
            Some("running") => {
                let remaining = deadline.saturating_duration_since(Instant::now());
                if remaining.is_zero() {
                    return Ok(Refresh::Pending {
                        waited_secs: begun.elapsed().as_secs(),
                    });
                }
                std::thread::sleep(remaining.min(Duration::from_secs(1)));
            }
            Some("failed") => return Err("keeper merge index refresh failed".into()),
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
    let mut scheduled = false;
    let mut continuation = false;
    let mut state = None;
    let mut project = None;
    let mut rest = args.iter();
    while let Some(arg) = rest.next() {
        match arg.as_str() {
            "--count" => count = true,
            "--repos" => repos = true,
            "--deploy-status" => deploy_status = true,
            "--scheduled" => scheduled = true,
            "--continue" => continuation = true,
            "--state" => match rest.next() {
                Some(path) => state = Some(std::path::PathBuf::from(path)),
                None => return usage(),
            },
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
    // The timer's mode owns the pending exit policy; the numeric and preview flags never see it.
    if scheduled {
        return match state {
            Some(state) if !(count || repos || deploy_status) => {
                crate::merge_close_unit::run_unit(env, &state, continuation, project)
            }
            _ => usage(),
        };
    }
    if continuation || state.is_some() {
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
        "usage: plangraph merge-close [--count | --repos | --deploy-status] [--project NAME] | merge-close --scheduled --state PATH [--continue] [--project NAME]"
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
    if let Refresh::Pending { .. } = refresh_index(control, refresh_timeout, false)? {
        return Err(REFRESH_STILL_RUNNING.into());
    }
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn deadline_keeper_caps_the_timeout_passed_to_the_request() {
        let env = Env::new();
        let now = Instant::now();
        for (request_limit, remaining, expected) in [
            (10, Duration::from_secs(5), 5),
            (10, Duration::from_secs(400), 10),
            (MAX_TIME_SECS, Duration::from_secs(5), 5),
            (MAX_TIME_SECS, Duration::from_millis(1), 1),
        ] {
            let transport = DeadlineKeeper {
                keeper: SlowKeeper {
                    env: &env,
                    max_time_secs: request_limit,
                },
                deadline: now + remaining,
            };
            let reply = transport.call_with(now, |keeper| {
                assert_eq!(keeper.max_time_secs, expected);
                Ok(json!({"merged_not_done": 0}))
            });
            assert_eq!(reply, Ok(json!({"merged_not_done": 0})));
        }
    }

    #[test]
    fn deadline_keeper_does_not_call_the_request_at_the_deadline() {
        let env = Env::new();
        let deadline = Instant::now();
        let transport = DeadlineKeeper {
            keeper: SlowKeeper {
                env: &env,
                max_time_secs: MAX_TIME_SECS,
            },
            deadline,
        };
        let reply = transport.call_with(deadline, |_| panic!("request started at the deadline"));
        assert_eq!(reply, Err(MERGE_INDEX_EXPIRED.into()));
    }
}
