//! Binds one daemon run to a durable slot of `<state>/slots.json` before its launcher call.
//!
//! The local supervisor gate stays the one admission queue; this seam mirrors it into the
//! shared slot table so a restart (and every other reader of the table) sees what the daemon
//! holds:
//!
//! - **pending**: the run waits in the local queue; a [`Ticket`] owned by this daemon's
//!   `(pid, start_time)` is in `queued`. Nothing is launched.
//! - **bound**: the run was admitted; its ticket became an [`ActiveSlot`] with `bound = true`,
//!   still owned by the daemon identity and naming the confinement unit it is about to create.
//!   [`SlotBind::reserve`] must succeed before the launcher is called; a refusal ends the run
//!   without a launcher call.
//! - **launched**: [`SlotBind::launched`] re-keys the row to the process identity of the run,
//!   the key the restart reconciliation matches a run record against.
//!
//! The monitor persists `launch_started` in the run record before it calls the launcher; the
//! launcher may spawn before a scope exists (and a plain launch never has one), so from that
//! point the outcome of a crash is unknown and the row stays held. A daemon that dies after the
//! bind but before that marker left a row with no process behind it. [`release_pre_exec`] frees
//! such a row only on proof (dead owner, a run record with neither a process nor the marker,
//! and a [`StartProof`] that no scope exists); a live owner, a launch that began or an answer of
//! "unknown" keeps it.
use super::{group::unit_name, record::RunRecord};
use crate::{
    hosts::Role,
    slots::{self, ActiveSlot, GroupLiveness, SlotClass, SlotError, Ticket},
};
use std::{
    collections::HashMap,
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
    time::Duration,
};

const LOCK_WAIT: Duration = Duration::from_secs(2);
/// Record field the monitor sets, durably, right before the launcher call.
pub const LAUNCH_STARTED: &str = "launch_started";
/// Record field naming the daemon that created a queued record.
pub const SLOT_OWNER: &str = "slot_owner";
/// File in the run directory that claims the run id for one daemon.
const CLAIM_FILE: &str = "claim";

/// The daemon's own identity and the slot every run it starts asks for.
#[derive(Debug, Clone)]
pub struct SlotBindCfg {
    /// Directory holding `slots.json`.
    pub slots_dir: PathBuf,
    pub daemon_pid: u32,
    /// Opaque start time of the daemon process (see [`ActiveSlot::start_time`]).
    pub daemon_start_time: u64,
    pub role: Role,
    pub class: SlotClass,
    pub repo_id: String,
    pub reservation_mb: u64,
}

#[derive(Debug, thiserror::Error)]
pub enum BindError {
    #[error("slot table: {0}")]
    Table(#[from] SlotError),
    #[error("run id already holds a slot")]
    Held,
    #[error("run id already has a run directory")]
    Exists,
    #[error("run directory: {0}")]
    Dir(String),
}

pub(super) struct SlotBind {
    cfg: SlotBindCfg,
    /// The `(pid, start_time)` each bound run's row is currently keyed by.
    keys: Mutex<HashMap<String, (u32, u64)>>,
}

fn now_ms() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |d| u64::try_from(d.as_millis()).unwrap_or(u64::MAX))
}

impl SlotBind {
    pub(super) fn new(cfg: SlotBindCfg) -> Arc<Self> {
        Arc::new(Self {
            cfg,
            keys: Mutex::new(HashMap::new()),
        })
    }
    fn owner(&self) -> (u32, u64) {
        (self.cfg.daemon_pid, self.cfg.daemon_start_time)
    }
    /// The `{pid, start_time}` the queued record carries, so recovery can tell a start this
    /// daemon abandoned (and prove it dead) from every other queued record.
    pub(super) fn owner_value(&self) -> serde_json::Value {
        let (pid, start_time) = self.owner();
        serde_json::json!({"pid": pid, "start_time": start_time})
    }
    /// Claim `run_id` exclusively before anything of the run is prepared. The claim is a
    /// `claim` file created with `create_new` in the run directory (the daemon already writes
    /// its prompt there, so the directory itself is not the token). A run that already has a
    /// record, or a claim of another owner, is refused and nothing of it is touched; the
    /// claimant's own earlier claim is re-entered, so the daemon claims at the head of its
    /// start and the supervisor claims again when the start reaches it. No slot of another
    /// owner may hold the id. Read-only on the slot table.
    pub(super) fn claim_run(&self, run_id: &str) -> Result<(), BindError> {
        let dir = super::record::run_dir(&self.cfg.slots_dir, run_id)
            .map_err(|e| BindError::Dir(e.to_string()))?;
        let dir_error = |e: std::io::Error| BindError::Dir(e.to_string());
        std::fs::create_dir_all(&dir).map_err(dir_error)?;
        if dir.join("record.json").exists() {
            return Err(BindError::Exists);
        }
        let owner = self.owner_value().to_string();
        let claim = dir.join(CLAIM_FILE);
        let created = match std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&claim)
        {
            Ok(mut file) => {
                use std::io::Write;
                file.write_all(owner.as_bytes()).map_err(dir_error)?;
                true
            }
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {
                // Only this daemon's own earlier claim may be re-entered.
                if std::fs::read_to_string(&claim).ok().as_deref() != Some(owner.as_str()) {
                    return Err(BindError::Exists);
                }
                false
            }
            Err(e) => return Err(dir_error(e)),
        };
        let (pid, start) = self.owner();
        let held = slots::with_table(&self.cfg.slots_dir, LOCK_WAIT, |table| {
            table.active.iter().any(|s| s.run_id == run_id)
                || table
                    .queued
                    .iter()
                    .any(|t| t.run_id == run_id && (t.pid, t.start_time) != (pid, start))
        });
        match held {
            Ok(false) => Ok(()),
            other => {
                if created {
                    let _ = std::fs::remove_file(&claim);
                }
                other?;
                Err(BindError::Held)
            }
        }
    }
    /// Persist the pending entry of a run that waits in the local queue, under the role of the
    /// run (the role of the run record, which restart reconciliation compares).
    pub(super) fn pending(&self, run_id: &str, role: Option<Role>) -> Result<(), BindError> {
        let ticket = Ticket {
            run_id: run_id.into(),
            pid: self.cfg.daemon_pid,
            start_time: self.cfg.daemon_start_time,
            enqueued_ms: now_ms(),
            role: role.unwrap_or(self.cfg.role),
            class: self.cfg.class,
            repo_id: self.cfg.repo_id.clone(),
            reservation_mb: self.cfg.reservation_mb,
        };
        slots::with_table(&self.cfg.slots_dir, LOCK_WAIT, |table| {
            if table.active.iter().any(|s| s.run_id == run_id) {
                return Err(BindError::Held);
            }
            if !table.queued.iter().any(|t| t.run_id == run_id) {
                table.queued.push(ticket);
            }
            Ok(())
        })?
    }
    /// Drop the pending entry of a run that left the queue without a bind.
    pub(super) fn withdraw(&self, run_id: &str) {
        let (pid, start) = self.owner();
        // A claim that never got a record goes; a run that has one keeps everything.
        if let Ok(dir) = super::record::run_dir(&self.cfg.slots_dir, run_id) {
            let claim = dir.join(CLAIM_FILE);
            let ours = std::fs::read_to_string(&claim).ok().as_deref()
                == Some(self.owner_value().to_string().as_str());
            if ours && !dir.join("record.json").exists() {
                let _ = std::fs::remove_file(claim);
            }
        }
        let _ = slots::with_table(&self.cfg.slots_dir, LOCK_WAIT, |table| {
            table
                .queued
                .retain(|t| !(t.run_id == run_id && t.pid == pid && t.start_time == start));
        });
    }
    /// Turn the pending entry into the bound reservation. Refused when another owner holds a
    /// slot of this run id or the table cannot be written: the caller must not launch.
    pub(super) fn reserve(self: &Arc<Self>, run_id: &str) -> Result<Reservation, BindError> {
        let (pid, start) = self.owner();
        let created_ms = now_ms();
        slots::with_table(&self.cfg.slots_dir, LOCK_WAIT, |table| {
            if table.active.iter().any(|s| s.run_id == run_id) {
                return Err(BindError::Held);
            }
            let own = |t: &Ticket| t.run_id == run_id && t.pid == pid && t.start_time == start;
            let ticket = table.queued.iter().find(|t| own(t)).cloned();
            table.queued.retain(|t| !own(t));
            table.active.push(ActiveSlot {
                run_id: run_id.into(),
                pid,
                start_time: start,
                role: ticket.as_ref().map_or(self.cfg.role, |t| t.role),
                class: self.cfg.class,
                repo_id: self.cfg.repo_id.clone(),
                seat_slot: None,
                group: Some(unit_name(run_id)),
                reservation_mb: ticket.map_or(self.cfg.reservation_mb, |t| t.reservation_mb),
                created_ms,
                bound: true,
            });
            Ok(())
        })??;
        self.keys
            .lock()
            .unwrap()
            .insert(run_id.into(), (pid, start));
        Ok(Reservation {
            bind: self.clone(),
            run_id: run_id.into(),
        })
    }
    /// Re-key the row of `run_id` to the process the launcher started.
    pub(super) fn launched(&self, run_id: &str, pid: u32, start_time: u64, group: Option<&str>) {
        let Some(old) = self.keys.lock().unwrap().get(run_id).copied() else {
            return;
        };
        let changed = slots::with_table(&self.cfg.slots_dir, LOCK_WAIT, |table| {
            let row = table
                .active
                .iter_mut()
                .find(|s| s.run_id == run_id && (s.pid, s.start_time) == old);
            row.is_some_and(|row| {
                row.pid = pid;
                row.start_time = start_time;
                if let Some(group) = group {
                    row.group = Some(group.into());
                }
                true
            })
        });
        if changed.unwrap_or(false) {
            self.keys
                .lock()
                .unwrap()
                .insert(run_id.into(), (pid, start_time));
        }
    }
}

/// The bound row of one run; dropping it frees the slot and wakes the waiters.
pub(super) struct Reservation {
    bind: Arc<SlotBind>,
    run_id: String,
}
impl Drop for Reservation {
    fn drop(&mut self) {
        let key = self.bind.keys.lock().unwrap().remove(&self.run_id);
        let Some((pid, start)) = key else { return };
        let freed = slots::with_table(&self.bind.cfg.slots_dir, LOCK_WAIT, |table| {
            slots::release(table, &self.run_id, pid, start)
        });
        if freed.unwrap_or(false) {
            let _ = slots::touch_wake(&self.bind.cfg.slots_dir);
        }
    }
}

/// Whether a run's process (or its confinement scope) was ever started.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Started {
    /// A process or a scope of the run exists or existed.
    Yes,
    /// Proven: nothing of this run was ever started.
    No,
    /// The probe could not tell; the row stays.
    Unknown,
}

/// Evidence about processes started for one run, beyond its record.
pub trait StartProof {
    /// Does any confinement scope of `run_id` exist?
    fn scope_started(&self, run_id: &str) -> Started;
}

/// Free the pre-exec rows (pending tickets and bound reservations) that a dead daemon left,
/// each only on proof that no process of it was started. Returns the freed run ids.
/// `live` answers whether the owning daemon is still alive; `proof` whether a scope exists.
pub fn release_pre_exec(
    slots_dir: &Path,
    live: &impl GroupLiveness,
    proof: &impl StartProof,
) -> Result<Vec<String>, SlotError> {
    let never_started = |run_id: &str, pid: u32, start: u64| -> bool {
        if live.process_alive(pid, start) {
            return false;
        }
        match RunRecord::read(slots_dir, run_id) {
            Ok(record) => {
                if ["pid", "proc", "pid_start", LAUNCH_STARTED]
                    .iter()
                    .any(|key| record.get(key).is_some_and(|v| !v.is_null()))
                {
                    return false;
                }
            }
            // A missing or unreadable record is no proof.
            Err(_) => return false,
        }
        proof.scope_started(run_id) == Started::No
    };
    let freed = slots::with_table(slots_dir, LOCK_WAIT, |table| {
        let mut freed = Vec::new();
        table.active.retain(|s| {
            // Only a bound row still keyed by an owner without a launched process.
            let pre_exec = s.bound && s.group.as_deref() == Some(unit_name(&s.run_id).as_str());
            let keep = !(pre_exec && never_started(&s.run_id, s.pid, s.start_time));
            if !keep {
                freed.push(s.run_id.clone());
            }
            keep
        });
        table.queued.retain(|t| {
            let keep = !never_started(&t.run_id, t.pid, t.start_time);
            if !keep {
                freed.push(t.run_id.clone());
            }
            keep
        });
        freed
    })?;
    if !freed.is_empty() {
        slots::touch_wake(slots_dir)?;
    }
    let mut freed = freed;
    freed.extend(settle_abandoned(slots_dir, live, &never_started));
    Ok(freed)
}

/// End the queued records of starts a dead daemon abandoned between the record and its ticket:
/// the record names its owner (`slot_owner`), the owner is dead and nothing of the run started.
/// Records without an owner, with a live owner or in doubt stay as they are.
fn settle_abandoned(
    slots_dir: &Path,
    live: &impl GroupLiveness,
    never_started: &impl Fn(&str, u32, u64) -> bool,
) -> Vec<String> {
    let Ok(entries) = std::fs::read_dir(slots_dir.join("runs")) else {
        return Vec::new();
    };
    let mut settled = Vec::new();
    for entry in entries.flatten() {
        let Ok(mut record) = RunRecord::read_path(entry.path().join("record.json")) else {
            continue;
        };
        let owner = record.get(SLOT_OWNER).and_then(|o| {
            Some((
                u32::try_from(o.get("pid")?.as_u64()?).ok()?,
                o.get("start_time")?.as_u64()?,
            ))
        });
        let (Some((pid, start)), Some(super::record::Status::Queued)) = (owner, record.status())
        else {
            continue;
        };
        let run_id = entry.file_name().to_string_lossy().into_owned();
        if live.process_alive(pid, start) || !never_started(&run_id, pid, start) {
            continue;
        }
        let now = super::finalize::now();
        record.set("error", serde_json::json!("abandoned before launch"));
        record.set("reason", serde_json::json!("abandoned before launch"));
        if record
            .transition(super::record::Status::Failed, now)
            .is_ok()
            && record.save().is_ok()
        {
            settled.push(run_id);
        }
    }
    settled
}
