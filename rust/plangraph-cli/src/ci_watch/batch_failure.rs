//! A failing pull request head that belongs to a batch or train with a recorded owner is addressed to the
//! batch, not to a session: delivered with the job log to the batch's current writer (the recorded writer or
//! the successor that inherited the batch), kept open until acknowledged, escalated to plan-order after 15
//! minutes. The watcher never starts a writer and never drops the failure (rule 3.63 (3)).
use super::main_freeze::Alert;
use super::Task;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;

/// An unacknowledged failure escalates to plan-order after this long (seconds).
pub const ESCALATE_SECS: i64 = 15 * 60;

/// The writer-to-batch record: which batch a pull request belongs to, who writes it now, who inherited it.
#[derive(Default, Deserialize, Serialize)]
pub struct Owners {
    /// pull request number -> batch name
    #[serde(default)]
    pub prs: BTreeMap<String, String>,
    /// batch name -> the writer it was recorded with
    #[serde(default)]
    pub writers: BTreeMap<String, String>,
    /// handed-off writer -> successor that inherited its batches
    #[serde(default)]
    pub successors: BTreeMap<String, String>,
}

impl Owners {
    pub fn batch_of(&self, pr: u64) -> Option<&str> {
        self.prs.get(&pr.to_string()).map(String::as_str)
    }
    /// The current writer: the recorded one, followed through every hand-off.
    pub fn writer_of(&self, batch: &str) -> Option<String> {
        let mut writer = self.writers.get(batch)?.clone();
        for _ in 0..16 {
            match self.successors.get(&writer) {
                Some(next) if *next != writer => writer = next.clone(),
                _ => break,
            }
        }
        Some(writer)
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct FailureRecord {
    pub id: String,
    pub batch: String,
    pub pr: u64,
    pub run_id: u64,
    pub job: String,
    pub test: String,
    pub excerpt: String,
    pub created_at: i64,
    pub delivered_to: Option<String>,
    pub acked: bool,
    pub escalated: bool,
}

pub trait Deliver {
    fn send(&mut self, writer: &str, record: &FailureRecord) -> Result<(), String>;
}

pub fn record_id(batch: &str, pr: u64, test: &str, run_id: u64) -> String {
    let digest = Sha256::digest(format!("{batch}\0{pr}\0{test}\0{run_id}").as_bytes());
    format!("batch-failure-{:x}", digest)[..30].to_owned()
}

/// Inserts the failure of a batch's pull request if it is not recorded yet; returns its id, or `None` when the
/// pull request belongs to no recorded batch (the unchanged intake path applies). Nothing is delivered here.
pub fn record_failure(
    records: &mut Vec<FailureRecord>,
    owners: &Owners,
    task: &Task,
    now: i64,
) -> Option<String> {
    let batch = task.pr.and_then(|pr| owners.batch_of(pr))?;
    let pr = task.pr.unwrap_or(0);
    let id = record_id(batch, pr, &task.test, task.run_id);
    if !records.iter().any(|r| r.id == id) {
        records.push(FailureRecord {
            id: id.clone(),
            batch: batch.to_owned(),
            pr,
            run_id: task.run_id,
            job: task.job.clone(),
            test: task.test.clone(),
            excerpt: task.excerpt.clone(),
            created_at: now,
            delivered_to: None,
            acked: false,
            escalated: false,
        });
    }
    Some(id)
}

/// The open records that still have to reach their batch's current writer: (record id, writer).
pub fn pending_deliveries(records: &[FailureRecord], owners: &Owners) -> Vec<(String, String)> {
    records
        .iter()
        .filter(|r| !r.acked)
        .filter_map(|r| {
            let writer = owners.writer_of(&r.batch)?;
            (r.delivered_to.as_deref() != Some(writer.as_str())).then(|| (r.id.clone(), writer))
        })
        .collect()
}

/// Records a delivery; an acknowledgement that arrived meanwhile is kept.
pub fn mark_delivered(records: &mut [FailureRecord], id: &str, writer: &str) {
    if let Some(r) = records.iter_mut().find(|r| r.id == id) {
        r.delivered_to = Some(writer.to_owned());
    }
}

/// Escalates the records that stayed unacknowledged for 15 minutes to plan-order, once. Nothing is launched.
pub fn escalate_overdue(
    records: &mut [FailureRecord],
    now: i64,
    alerts: &mut BTreeMap<String, Alert>,
) -> Vec<String> {
    let mut out = Vec::new();
    for record in records.iter_mut().filter(|r| !r.acked) {
        if now - record.created_at >= ESCALATE_SECS && !record.escalated {
            record.escalated = true;
            let id = format!("batch_failure_unacked:{}", record.id);
            alerts.insert(
                id.clone(),
                Alert {
                    id,
                    kind: "batch_failure_unacked".into(),
                    due: "plan-order".into(),
                    detail: format!(
                        "batch {} pr {} run {} test {}: no acknowledgement in 15 minutes; restart or resume its writer",
                        record.batch, record.pr, record.run_id, record.test
                    ),
                    raised_at: now,
                },
            );
            out.push(format!("escalated {} to plan-order", record.id));
        }
    }
    out
}

fn deliver_open(
    record: &mut FailureRecord,
    owners: &Owners,
    deliver: &mut impl Deliver,
) -> Option<String> {
    let (_, writer) = pending_deliveries(std::slice::from_ref(record), owners)
        .into_iter()
        .next()?;
    match deliver.send(&writer, record) {
        Ok(()) => {
            record.delivered_to = Some(writer.clone());
            Some(format!("delivered {} to {writer}", record.id))
        }
        Err(_) => None,
    }
}

/// Records the failure (persisted before any delivery), then tries to deliver it.
pub fn route(
    records: &mut Vec<FailureRecord>,
    owners: &Owners,
    task: &Task,
    now: i64,
    deliver: &mut impl Deliver,
    mut save: impl FnMut(&[FailureRecord]) -> Result<(), String>,
) -> Result<Option<String>, String> {
    let Some(id) = record_failure(records, owners, task, now) else {
        return Ok(None);
    };
    save(records)?;
    if let Some(record) = records.iter_mut().find(|r| r.id == id) {
        deliver_open(record, owners, deliver);
    }
    save(records)?;
    Ok(Some(id))
}

/// Acknowledges a failure; the writer calls this when it took the failure up.
pub fn acknowledge(records: &mut [FailureRecord], id: &str) -> bool {
    records
        .iter_mut()
        .find(|r| r.id == id)
        .map(|r| r.acked = true)
        .is_some()
}

/// Retries delivery to the current writer of every open record and escalates the overdue ones.
pub fn sweep(
    records: &mut [FailureRecord],
    owners: &Owners,
    now: i64,
    deliver: &mut impl Deliver,
    alerts: &mut BTreeMap<String, Alert>,
) -> Vec<String> {
    let mut out = Vec::new();
    for record in records.iter_mut() {
        out.extend(deliver_open(record, owners, deliver));
    }
    out.extend(escalate_overdue(records, now, alerts));
    out
}
