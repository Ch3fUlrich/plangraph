//! `plangraph merge-close`: the keeper closes every item a merge into main names.
//!
//! The timer refreshes the merge index, then closes each open,
//! claimed or in-review item named by a `Plan-Item` trailer: no router, no decision id.
//! The job prints one line per closed item and one line per key the keeper refused or
//! flagged, with the reason, so routing sees every merged key that stayed open.
//! A timer runs it every ten minutes, and the batch land step calls [`run_once`].
use crate::feeder::{Env, SlowKeeper, Transport, MERGE_INDEX_EXPIRED, REQUEST_TIMED_OUT};
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::fmt::Write as _;
use std::time::{Duration, Instant};

/// Keep a bound on each keeper call, including a page of close results.
const MAX_TIME_SECS: u32 = 300;
const REFRESH_TIMEOUT: Duration = Duration::from_secs(95);
const REFRESH_STILL_RUNNING: &str = "merge index refresh is still running";
const SKIP_LINE: &str = "merge-close: skip, merge index refresh still running (next run retries)";
// Leave time for process overhead and logging before the service's 420 s timeout.
const PASS_BUDGET: Duration = Duration::from_secs(400);
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
}

impl ScheduleTiming {
    pub fn new(refresh_timeout: Duration, budget: Duration) -> Self {
        Self {
            refresh_timeout,
            deadline: Instant::now() + budget,
        }
    }
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
        self.call_with(Instant::now(), |keeper, limit| {
            keeper.call_within(verb, body, limit)
        })
    }

    fn call_until(&self, verb: &str, body: Option<Value>, until: Instant) -> Result<Value, String> {
        let deadline = self.deadline.min(until);
        let reply = self.call_by(Instant::now(), until, |keeper, limit| {
            keeper.call_within(verb, body, limit)
        })?;
        // A valid refresh answer that came after its wait ended does not start a close. An
        // answer that is not a scan status stays a reply, so the caller rejects it as invalid.
        if Instant::now() > deadline && matches!(reply["scan"].as_str(), Some("ready" | "running"))
        {
            return Err(MERGE_INDEX_EXPIRED.into());
        }
        Ok(reply)
    }
}

impl DeadlineKeeper<'_> {
    fn call_with(
        &self,
        now: Instant,
        request: impl FnOnce(&SlowKeeper<'_>, Duration) -> Result<Value, String>,
    ) -> Result<Value, String> {
        self.call_by(now, self.deadline, request)
    }

    /// Run one request that must end by the earlier of `until` and the pass deadline.
    /// The request gets the exact time left, so it cannot outlast the deadline.
    fn call_by(
        &self,
        now: Instant,
        until: Instant,
        request: impl FnOnce(&SlowKeeper<'_>, Duration) -> Result<Value, String>,
    ) -> Result<Value, String> {
        let deadline = self.deadline.min(until);
        let remaining = deadline.saturating_duration_since(now);
        if remaining.is_zero() {
            return Err(MERGE_INDEX_EXPIRED.into());
        }
        let own_limit = Duration::from_secs(u64::from(self.keeper.max_time_secs));
        let result = request(&self.keeper, own_limit.min(remaining));
        match result {
            // Only the request's own time limit means the index ran out; a refusal that
            // arrives late is still a refusal.
            Err(error)
                if error == REQUEST_TIMED_OUT
                    && remaining <= own_limit
                    && Instant::now() >= deadline =>
            {
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
    run_timed(env, apply, project, deployment_only, timing)
}

/// The scheduled pass over the real keeper transport with the caller's refresh limit and budget.
pub fn run_timed(
    env: &Env,
    apply: bool,
    project: Option<&str>,
    deployment_only: bool,
    timing: ScheduleTiming,
) -> Result<Outcome, String> {
    let seats = require_seats(env)?;
    let transport = |max_time_secs| DeadlineKeeper {
        keeper: SlowKeeper { env, max_time_secs },
        deadline: timing.deadline,
    };
    run_scheduled_with(
        &transport(MAX_TIME_SECS),
        &transport(10),
        seats,
        apply,
        project,
        deployment_only,
        timing,
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
    timing: ScheduleTiming,
) -> Result<Outcome, String> {
    let ScheduleTiming {
        refresh_timeout,
        deadline,
    } = timing;
    if refresh_index(control, refresh_timeout)? == Refresh::Pending {
        return if apply {
            Ok(skipped(String::new(), 0))
        } else {
            Err(REFRESH_STILL_RUNNING.into())
        };
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
    let mut refresh_pending = false;
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
                    // An expired cursor is never retried on the old index.
                    if refresh_index(control, refresh_timeout.min(remaining))? == Refresh::Pending {
                        refresh_pending = true;
                        return Err(error);
                    }
                    call()
                }
                result => result,
            }
        });
        if let Err(error) = result {
            let skip = apply && refresh_pending;
            if !apply || error != MERGE_INDEX_EXPIRED || !(pass.closed_any || skip) {
                return Err(error);
            }
            if pass.closed_any {
                let remaining = projects.len() - index;
                let _ = writeln!(
                    pass.text,
                    "continue\t{remaining} projects remaining\t{error}"
                );
            }
            if skip {
                // Keep the rows the earlier pages closed and say why the pass stopped.
                let Outcome {
                    text,
                    merged_not_done,
                } = pass.finish(deployment_only);
                return Ok(skipped(text, merged_not_done));
            }
            break;
        }
    }
    if !apply && !pass.scan_errors.is_empty() {
        return Err(format!("incomplete merge scan: {:?}", pass.scan_errors));
    }
    Ok(pass.finish(deployment_only))
}

/// How a bounded wait for the merge index refresh ended.
#[derive(Debug, PartialEq)]
enum Refresh {
    Ready,
    /// Another run's refresh was still running when the wait ended.
    Pending,
}

/// The finished text of a pass that stopped on a running refresh, with the skip line last.
fn skipped(mut text: String, merged_not_done: u64) -> Outcome {
    let _ = writeln!(text, "{SKIP_LINE}");
    Outcome {
        text,
        merged_not_done,
    }
}

/// Start a refresh and wait for it until `timeout` has passed. Every request ends by then.
fn refresh_index(control: &dyn Transport, timeout: Duration) -> Result<Refresh, String> {
    let deadline = Instant::now() + timeout;
    let request = |refresh: &str| {
        match control.call_until(
            "close_merged_auto",
            Some(json!({ "refresh": refresh })),
            deadline,
        ) {
            // A request cut off by the wait's own deadline ends the wait, not the pass.
            Err(error) if error == MERGE_INDEX_EXPIRED && Instant::now() >= deadline => Ok(None),
            result => result.map(Some),
        }
    };
    // The start reply carries the same scan status as a poll, and is checked the same way.
    match request("start")? {
        None => return Ok(Refresh::Pending),
        Some(reply) if matches!(reply["scan"].as_str(), Some("ready" | "running")) => {}
        Some(_) => return Err("keeper reply has no valid merge index status".into()),
    }
    loop {
        let Some(reply) = request("status")? else {
            return Ok(Refresh::Pending);
        };
        match reply["scan"].as_str() {
            Some("ready") => return Ok(Refresh::Ready),
            Some("running") => {
                let remaining = deadline.saturating_duration_since(Instant::now());
                if remaining.is_zero() {
                    return Ok(Refresh::Pending);
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
    // The watcher state is only written from a fresh index, so a refresh that is still running fails.
    if refresh_index(control, refresh_timeout)? == Refresh::Pending {
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

    fn keeper(env: &Env, request_limit: u32, deadline: Instant) -> DeadlineKeeper<'_> {
        DeadlineKeeper {
            keeper: SlowKeeper {
                env,
                max_time_secs: request_limit,
            },
            deadline,
        }
    }

    #[test]
    fn deadline_keeper_caps_the_timeout_passed_to_the_request() {
        let env = Env::new();
        let now = Instant::now();
        for (request_limit, remaining, expected) in [
            (10, Duration::from_secs(5), Duration::from_secs(5)),
            (10, Duration::from_secs(400), Duration::from_secs(10)),
            (
                MAX_TIME_SECS,
                Duration::from_secs(5),
                Duration::from_secs(5),
            ),
            (
                MAX_TIME_SECS,
                Duration::from_millis(1),
                Duration::from_millis(1),
            ),
            (10, Duration::from_millis(1500), Duration::from_millis(1500)),
        ] {
            let transport = keeper(&env, request_limit, now + remaining);
            let reply = transport.call_with(now, |_, limit| {
                assert_eq!(limit, expected);
                Ok(json!({"merged_not_done": 0}))
            });
            assert_eq!(reply, Ok(json!({"merged_not_done": 0})));
        }
    }

    #[test]
    fn deadline_keeper_does_not_call_the_request_at_the_deadline() {
        let env = Env::new();
        let deadline = Instant::now();
        let transport = keeper(&env, MAX_TIME_SECS, deadline);
        let reply = transport.call_with(deadline, |_, _| panic!("request started at the deadline"));
        assert_eq!(reply, Err(MERGE_INDEX_EXPIRED.into()));
    }

    #[test]
    fn a_request_ends_by_the_earlier_of_the_refresh_and_pass_deadlines() {
        let env = Env::new();
        let now = Instant::now();
        let transport = keeper(&env, 10, now + Duration::from_secs(400));
        let reply = transport.call_by(now, now + Duration::from_secs(3), |_, limit| {
            assert_eq!(limit, Duration::from_secs(3));
            Ok(json!({}))
        });
        assert_eq!(reply, Ok(json!({})));
        let late = transport.call_by(now, now, |_, _| {
            panic!("request started at the wait deadline")
        });
        assert_eq!(late, Err(MERGE_INDEX_EXPIRED.into()));
    }

    #[test]
    fn only_a_timeout_at_the_deadline_is_index_expiry() {
        let env = Env::new();
        let now = Instant::now();
        // The deadline is already behind us when the request fails, as after a slow reply.
        let transport = keeper(&env, 10, now + Duration::from_millis(1));
        std::thread::sleep(Duration::from_millis(5));
        for (error, expected) in [
            (REQUEST_TIMED_OUT, MERGE_INDEX_EXPIRED),
            (
                "keeper request refused or unreachable",
                "keeper request refused or unreachable",
            ),
            ("item_refused:conflict", "item_refused:conflict"),
        ] {
            let reply = transport.call_with(now, |_, _| Err(error.into()));
            assert_eq!(reply, Err(expected.into()));
        }
    }
}
