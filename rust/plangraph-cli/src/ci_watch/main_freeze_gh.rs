//! The GitHub, keeper and file effects of the main-freeze handler, and its wiring into `plangraph ci-watch`.
use super::batch_failure::{self, Deliver, FailureRecord, Owners};
use super::github::Github;
use super::main_freeze::{self, Alert, Diagnosis, Effects, FreezeState, Kind};
use super::{logs, Result, Task};
use crate::atomic;
use crate::land::main_state::Answer;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::path::Path;

pub struct GhEffects<
    'a,
    G: Github,
    F: FnMut(&mut Task) -> Result<bool>,
    C: FnMut(&str) -> Result<()>,
> {
    pub gh: &'a mut G,
    pub repo: String,
    pub dir: &'a Path,
    pub define: F,
    /// Cancels a fix task in the keeper by key.
    pub close: C,
}

impl<G: Github, F: FnMut(&mut Task) -> Result<bool>, C: FnMut(&str) -> Result<()>> Effects
    for GhEffects<'_, G, F, C>
{
    fn attempts(&mut self, run: u64) -> Result<(u32, bool)> {
        let latest = self.gh.value(&format!("actions/runs/{run}"))?["run_attempt"]
            .as_u64()
            .ok_or("run without attempt")? as u32;
        let info = self
            .gh
            .value(&format!("actions/runs/{run}/attempts/{latest}"))?;
        Ok((latest, info["status"] == "completed"))
    }
    fn latest_conclusion(&mut self, run: u64) -> Option<String> {
        let latest = self.attempts(run).ok()?.0;
        let info = self
            .gh
            .value(&format!("actions/runs/{run}/attempts/{latest}"))
            .ok()?;
        info["conclusion"].as_str().map(str::to_owned)
    }
    fn diagnose(&mut self, run: u64, attempt: u32) -> Diagnosis {
        let jobs = self
            .gh
            .pages(
                &format!("actions/runs/{run}/attempts/{attempt}/jobs?per_page=100"),
                Some("jobs"),
            )
            .unwrap_or_default();
        let mut found = Diagnosis::default();
        for job in jobs.iter().filter(|j| j["conclusion"] == "failure") {
            let Some(id) = job["id"].as_u64() else {
                found.incomplete = true;
                continue;
            };
            match self.gh.log(id) {
                Ok(log) => {
                    found.incomplete |= log.contains("[log truncated");
                    found.tests.extend(
                        logs::failures(&log)
                            .into_keys()
                            .map(|t| logs::scrub(&t))
                            .filter(|t| t != "<job-failure>"),
                    );
                }
                Err(_) => found.incomplete = true,
            }
        }
        found.incomplete |= found.tests.is_empty();
        found
    }
    fn request(&mut self, run: u64, kind: Kind) -> Result<()> {
        let verb = match kind {
            Kind::RerunFailed => "rerun-failed",
            Kind::RerunAll => "rerun",
            Kind::Cancel => "cancel",
        };
        self.gh.run_verb(run, verb)
    }
    fn fix_task_key(&self, run: u64, episode: u32, diagnosis: &Diagnosis) -> String {
        let test = diagnosis
            .tests
            .first()
            .cloned()
            .unwrap_or_else(|| "<job-failure>".into());
        // Each red episode of a run and test is its own task, and two runs never share one.
        let digest =
            Sha256::digest(format!("{}\0main\0{run}\0{test}\0{episode}", self.repo).as_bytes());
        format!("plangraph:CI-FIX-{digest:x}")
    }
    fn fix_task(&mut self, run: u64, episode: u32, diagnosis: &Diagnosis) -> Result<String> {
        let test = diagnosis
            .tests
            .first()
            .cloned()
            .unwrap_or_else(|| "<job-failure>".into());
        let key = self.fix_task_key(run, episode, diagnosis);
        let task_path = self.dir.join("tasks.jsonl");
        let mut tasks = super::load_tasks(&task_path)?;
        let old = tasks.get(&key).cloned();
        let task = Task {
            key: key.clone(),
            head: "main".into(),
            pr: None,
            run_id: run,
            job: "main".into(),
            test,
            excerpt: format!("main is red; failing tests {:?}", diagnosis.tests),
            priority: 10,
            keeper_defined: old
                .as_ref()
                .is_some_and(|t| t.keeper_defined && t.run_id == run),
            keeper_key: old.and_then(|t| t.keeper_key),
        };
        let mut output = Vec::new();
        let pending =
            super::store_task(&task_path, &mut tasks, task, &mut self.define, &mut output)?;
        output.into_iter().for_each(|l| println!("{l}"));
        if pending {
            // Persisted locally; the keeper has not confirmed it, so the handler tries again next pass.
            return Err("keeper delivery pending".into());
        }
        Ok(key)
    }
    fn close_fix_task(&mut self, key: &str) -> Result<()> {
        let keeper_key = super::load_tasks(&self.dir.join("tasks.jsonl"))?
            .get(key)
            .and_then(|t| t.keeper_key.clone())
            .unwrap_or_else(|| key.to_owned());
        (self.close)(&keeper_key)?;
        let path = self.dir.join("tasks.jsonl");
        let mut tasks = super::load_tasks(&path)?;
        tasks.remove(key);
        super::save_tasks(&path, &tasks)
    }
}

/// Delivery through the command named by `CI_WATCH_DELIVER_CMD` (`<cmd> <writer> <record file>`); without it
/// every delivery fails and the open record escalates to plan-order.
pub struct CommandDeliver<'a> {
    pub dir: &'a Path,
    /// Seconds a delivery may run: shorter than the claim's staleness.
    pub lease_secs: u64,
}

impl Deliver for CommandDeliver<'_> {
    fn send(&mut self, writer: &str, record: &FailureRecord) -> Result<()> {
        let cmd = std::env::var("CI_WATCH_DELIVER_CMD").map_err(|_| "no delivery command")?;
        let path = self.dir.join(format!("{}.delivery.json", record.id));
        save(&path, record)?;
        let mut child = std::process::Command::new(cmd)
            .arg(writer)
            .arg(&path)
            .spawn()
            .map_err(|e| e.to_string())?;
        // The command is bound to its claim's lease: past it, the claim may be taken over, so it is killed.
        let lease = std::time::Duration::from_secs(self.lease_secs);
        let started = std::time::Instant::now();
        loop {
            match child.try_wait().map_err(|e| e.to_string())? {
                Some(status) if status.success() => return Ok(()),
                Some(_) => return Err("delivery command failed".into()),
                None if started.elapsed() >= lease => {
                    let _ = child.kill();
                    let _ = child.wait();
                    return Err("delivery command exceeded its lease".into());
                }
                None => std::thread::sleep(std::time::Duration::from_millis(25)),
            }
        }
    }
}

fn save(path: &Path, value: &impl Serialize) -> Result<()> {
    atomic::write(path, &serde_json::to_vec(value).map_err(|e| e.to_string())?)
        .map_err(|e| e.to_string())
}

fn load<T: for<'d> Deserialize<'d> + Default>(path: &Path) -> Result<T> {
    match std::fs::read(path) {
        Ok(b) => {
            serde_json::from_slice(&b).map_err(|_| format!("invalid state {}", path.display()))
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(T::default()),
        Err(e) => Err(e.to_string()),
    }
}

pub fn load_owners(dir: &Path) -> Result<Owners> {
    load(&dir.join("owners.json"))
}

/// Read-modify-write of `batch-failures.json` under one lock, so intake, sweep and acknowledgement cannot
/// overwrite each other with an older copy.
pub fn update_failures<T>(
    dir: &Path,
    f: impl FnOnce(&mut Vec<FailureRecord>) -> Result<T>,
) -> Result<T> {
    std::fs::create_dir_all(dir).map_err(|e| e.to_string())?;
    let _lock = atomic::lock(&dir.join("batch-failures")).map_err(|e| e.to_string())?;
    let mut records = load_failures(dir)?;
    let out = f(&mut records)?;
    save_failures(dir, &records)?;
    Ok(out)
}

pub fn load_failures(dir: &Path) -> Result<Vec<FailureRecord>> {
    load(&dir.join("batch-failures.json"))
}

pub fn save_failures(dir: &Path, records: &[FailureRecord]) -> Result<()> {
    save(&dir.join("batch-failures.json"), &records)
}

/// Writes the open alerts where the flow watchdog's collector reads them for routing.
pub fn publish(dir: &Path, alerts: &BTreeMap<String, Alert>) -> Result<()> {
    let list: Vec<&Alert> = alerts.values().collect();
    save(
        &dir.join("alerts.json"),
        &serde_json::json!({ "alerts": list }),
    )
}

/// One handler pass: the batch-failure sweep, the main-freeze step, the alert file.
pub fn pass(
    dir: &Path,
    answer: &Answer,
    flakes: &BTreeMap<String, String>,
    fx: &mut impl Effects,
    now: i64,
) -> Result<Vec<String>> {
    let _lock = atomic::lock(&dir.join("main-freeze")).map_err(|e| e.to_string())?;
    let state_path = dir.join("main-freeze.json");
    let mut state: FreezeState = load(&state_path)?;
    let owners = load_owners(dir)?;
    let mut escalations = BTreeMap::new();
    let mut out = Vec::new();
    out.extend(deliver_pending(
        dir,
        &owners,
        &mut CommandDeliver {
            dir,
            lease_secs: (batch_failure::CLAIM_SECS as u64).saturating_sub(60),
        },
        now,
    )?);
    update_failures(dir, |records| {
        out.extend(batch_failure::escalate_overdue(
            records,
            now,
            &mut escalations,
        ));
        Ok(())
    })?;
    let records = load_failures(dir)?;
    out.extend(main_freeze::step(
        &mut state,
        answer,
        flakes,
        fx,
        false,
        |s| save(&state_path, s),
    )?);
    let mut open = state.alerts.clone();
    open.extend(escalations);
    // Escalations of earlier passes stay in the published file until acknowledged.
    for r in records.iter().filter(|r| r.escalated && !r.acked) {
        let id = format!("batch_failure_unacked:{}", r.id);
        open.entry(id.clone()).or_insert(Alert {
            id,
            kind: "batch_failure_unacked".into(),
            due: "plan-order".into(),
            detail: format!("batch {} pr {} run {}", r.batch, r.pr, r.run_id),
            raised_at: r.created_at,
        });
    }
    publish(dir, &open)?;
    Ok(out)
}

/// Delivers every open record to its batch's current writer. The lock is held to pick the work and to record
/// the result, never while the delivery command runs, so a command that acknowledges synchronously works and
/// an acknowledgement that arrives meanwhile is preserved.
pub fn deliver_pending(
    dir: &Path,
    owners: &Owners,
    deliver: &mut impl Deliver,
    now: i64,
) -> Result<Vec<String>> {
    // The work list is only a candidate list; each delivery is claimed under the lock immediately before it
    // is sent, so a queued delivery never starts after its claim expired.
    let candidates: Vec<(String, String)> = update_failures(dir, |records| {
        Ok(batch_failure::pending_deliveries(records, owners, now))
    })?;
    let mut lines = Vec::new();
    for (id, writer) in candidates {
        let claimed = update_failures(dir, |records| {
            let still = batch_failure::pending_deliveries(records, owners, now);
            if !still.iter().any(|(i, w)| *i == id && *w == writer) {
                return Ok(None);
            }
            let Some(token) = batch_failure::claim(records, &id, &writer, now) else {
                return Ok(None);
            };
            Ok(records
                .iter()
                .find(|r| r.id == id)
                .map(|r| (token, r.clone())))
        })?;
        let Some((token, record)) = claimed else {
            continue;
        };
        let ok = deliver.send(&writer, &record).is_ok();
        update_failures(dir, |records| {
            batch_failure::settle(records, &id, &writer, &token, ok);
            Ok(())
        })?;
        if ok {
            lines.push(format!("delivered {id} to {writer}"));
        }
    }
    Ok(lines)
}

/// Intake path: a failing batch head goes to the batch, everything else to the keeper fix lane.
pub fn route_or_define(
    dir: &Path,
    task: &mut Task,
    now: i64,
    keeper: impl FnOnce(&mut Task) -> Result<bool>,
) -> Result<bool> {
    let owners = load_owners(dir)?;
    // Recorded under the lock first; the delivery command runs outside it.
    let routed = update_failures(dir, |records| {
        Ok(batch_failure::record_failure(records, &owners, task, now))
    })?;
    if routed.is_some() {
        deliver_pending(
            dir,
            &owners,
            &mut CommandDeliver {
                dir,
                lease_secs: (batch_failure::CLAIM_SECS as u64).saturating_sub(60),
            },
            now,
        )?;
        // The record is open and addressed to the batch: no unassigned fix lane is created.
        return Ok(true);
    }
    keeper(task)
}
