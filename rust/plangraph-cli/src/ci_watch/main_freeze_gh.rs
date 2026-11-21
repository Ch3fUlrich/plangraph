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

pub struct GhEffects<'a, G: Github, F: FnMut(&mut Task) -> Result<bool>> {
    pub gh: &'a mut G,
    pub repo: String,
    pub dir: &'a Path,
    pub define: F,
}

impl<G: Github, F: FnMut(&mut Task) -> Result<bool>> Effects for GhEffects<'_, G, F> {
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
    fn fix_task(&mut self, run: u64, diagnosis: &Diagnosis) -> Result<()> {
        let test = diagnosis
            .tests
            .first()
            .cloned()
            .unwrap_or_else(|| "<job-failure>".into());
        let digest = Sha256::digest(format!("{}\0main\0{test}", self.repo).as_bytes());
        let key = format!("plangraph:CI-FIX-{digest:x}");
        let task_path = self.dir.join("tasks.jsonl");
        let mut tasks = super::load_tasks(&task_path)?;
        let old = tasks.get(&key).cloned();
        let task = Task {
            key,
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
        super::store_task(&task_path, &mut tasks, task, &mut self.define, &mut output)?;
        output.into_iter().for_each(|l| println!("{l}"));
        Ok(())
    }
}

/// Delivery through the command named by `CI_WATCH_DELIVER_CMD` (`<cmd> <writer> <record file>`); without it
/// every delivery fails and the open record escalates to plan-order.
pub struct CommandDeliver<'a> {
    pub dir: &'a Path,
}

impl Deliver for CommandDeliver<'_> {
    fn send(&mut self, writer: &str, record: &FailureRecord) -> Result<()> {
        let cmd = std::env::var("CI_WATCH_DELIVER_CMD").map_err(|_| "no delivery command")?;
        let path = self.dir.join(format!("{}.delivery.json", record.id));
        save(&path, record)?;
        let ok = std::process::Command::new(cmd)
            .arg(writer)
            .arg(&path)
            .status()
            .map_err(|e| e.to_string())?
            .success();
        if ok {
            Ok(())
        } else {
            Err("delivery command failed".into())
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
    let mut records = load_failures(dir)?;
    let owners = load_owners(dir)?;
    let mut escalations = BTreeMap::new();
    let mut out = batch_failure::sweep(
        &mut records,
        &owners,
        now,
        &mut CommandDeliver { dir },
        &mut escalations,
    );
    save_failures(dir, &records)?;
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

/// Intake path: a failing batch head goes to the batch, everything else to the keeper fix lane.
pub fn route_or_define(
    dir: &Path,
    task: &mut Task,
    now: i64,
    keeper: impl FnOnce(&mut Task) -> Result<bool>,
) -> Result<bool> {
    let owners = load_owners(dir)?;
    let mut records = load_failures(dir)?;
    let routed = batch_failure::route(
        &mut records,
        &owners,
        task,
        now,
        &mut CommandDeliver { dir },
        |r| save_failures(dir, r),
    )?;
    if routed.is_some() {
        // The record is open and addressed to the batch: no unassigned fix lane is created.
        return Ok(true);
    }
    keeper(task)
}
