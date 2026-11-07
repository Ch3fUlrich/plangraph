//! Daemon-owned startup: durable reconciliation, capability-gated reclaim, serve.
#![cfg_attr(not(target_os = "linux"), allow(dead_code))]
use crate::reconcile::ReconcileError;
use pg_worker_core::{lockfile::LockGuard, slots};
use std::{
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    time::Duration,
};

pub(crate) struct Guard {
    stop: Arc<AtomicBool>,
    thread: Option<std::thread::JoinHandle<()>>,
    // On: released after watcher joins. Off/shadow deliberately have no owner lock.
    _owner: Option<LockGuard>,
}
impl Drop for Guard {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Release);
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}
/// Only a complete inventory pass can issue this root-bound capability.
struct Reconciled {
    root: PathBuf,
    gone: Vec<(String, u32, u64, Option<String>)>,
}
struct Conservative;
impl slots::GroupLiveness for Conservative {
    fn group_state(&self, _: &str) -> slots::GroupState {
        slots::GroupState::Gone
    }
    fn process_alive(&self, _: u32, _: u64) -> bool {
        false
    }
}
impl slots::SeatLocks for Conservative {
    fn index_lock(&self, _: &str, _: u32) -> Option<PathBuf> {
        None
    }
    fn lock_state(&self, _: &Path) -> slots::LockState {
        slots::LockState::Unknown
    }
}
fn reclaim_after(root: &Path, proof: Reconciled, now: u64) -> Result<(), ReconcileError> {
    if proof.root != root {
        return Err(ReconcileError::Invalid);
    }
    let report = slots::with_table(root, Duration::from_secs(2), |table| {
        // Exclude pending, queued, live and unrecognized bindings entirely;
        // in particular the core's groupless dead-pid shortcut cannot run on them.
        let mut eligible = slots::SlotTable::default();
        let mut held = Vec::new();
        for slot in std::mem::take(&mut table.active) {
            if slot.bound
                && proof.gone.iter().any(|(run, pid, start, group)| {
                    run == &slot.run_id
                        && *pid == slot.pid
                        && *start == slot.start_time
                        && group == &slot.group
                })
            {
                eligible.active.push(slot);
            } else {
                held.push(slot);
            }
        }
        debug_assert!(eligible.active.iter().all(|slot| proof.gone.contains(&(
            slot.run_id.clone(),
            slot.pid,
            slot.start_time,
            slot.group.clone()
        ))));
        let report = slots::reclaim(
            &mut eligible,
            &Conservative,
            &Conservative,
            now,
            &slots::ReclaimPolicy::default(),
        );
        held.extend(eligible.active);
        table.active = held;
        report
    })
    .map_err(|_| ReconcileError::Io)?;
    if report.freed > 0 {
        slots::touch_wake(root).map_err(|_| ReconcileError::Io)?;
    }
    Ok(())
}
fn now() -> Result<u64, ReconcileError> {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .ok()
        .and_then(|d| u64::try_from(d.as_millis()).ok())
        .ok_or(ReconcileError::Invalid)
}

#[cfg(target_os = "linux")]
fn replay(root: &Path, store: &crate::evidence::EvidenceStore) -> Result<(), ReconcileError> {
    let runs = root.join("runs");
    if !runs.exists() {
        return Ok(());
    }
    let mut skipped = false;
    for dir in std::fs::read_dir(runs).map_err(|_| ReconcileError::Io)? {
        let path = dir
            .map_err(|_| ReconcileError::Io)?
            .path()
            .join("record.json");
        if !path.is_file() {
            skipped = true;
            continue;
        }
        let dir = path.parent().ok_or(ReconcileError::Invalid)?;
        let _record_lock = crate::reconcile_store::lock(dir, &dir.join("record.lock"))?;
        let value = crate::reconcile_store::read(&path)?;
        store.backfill(&[value]).map_err(|_| ReconcileError::Io)?;
    }
    if skipped {
        eprintln!("reconciliation replay skipped entries without record files");
    }
    Ok(())
}

pub(crate) fn configured(root: &Path) -> Result<Guard, String> {
    let mut guard = Guard {
        stop: Arc::new(AtomicBool::new(false)),
        thread: None,
        _owner: None,
    };
    // Keep the merged advisory config/shadow behavior. Only explicit on has
    // mutating startup authority in this rollout; off never enters reclaim.
    if std::env::var("PLANGRAPH_RELIABILITY_RECONCILE").as_deref() != Ok("on") {
        crate::reconcile::configured_startup(root)?;
        #[cfg(target_os = "linux")]
        if std::env::var("PLANGRAPH_RELIABILITY_RECONCILE").as_deref() == Ok("shadow") {
            for row in crate::reconcile_store::inventory(root).unwrap_or_else(|error| {
                eprintln!("shadow adoption unavailable: {error}");
                Vec::new()
            }) {
                if crate::reconcile_store::gone(row.identity.as_ref()).ok() == Some(false)
                    && row.identity.is_some_and(|identity| {
                        crate::reconcile_backend::native::Group::open(&row.run_id, identity).is_ok()
                    })
                {
                    eprintln!("would_adopt {} {}", row.run_id, row.attempt);
                }
            }
        }
        return Ok(guard);
    }
    std::fs::create_dir_all(root).map_err(|_| ReconcileError::Io.to_string())?;
    guard._owner = Some(
        crate::reconcile_store::lock(root, &root.join("reconcile.owner.lock"))
            .map_err(|e| e.to_string())?,
    );
    start(root, &mut guard).map_err(|e| e.to_string())?;
    Ok(guard)
}

#[cfg(target_os = "linux")]
fn start(root: &Path, guard: &mut Guard) -> Result<(), ReconcileError> {
    use crate::{
        reconcile_backend::native::Group,
        reconcile_watch::{Step, StopPolicy, Watch},
    };
    use pg_worker_core::{activity::Limits, run::record::RunRecord};
    let lock_deadline = std::time::Instant::now() + Duration::from_secs(10);
    let _lock = loop {
        match crate::reconcile_store::lock(root, &root.join("reconcile.lock")) {
            Ok(lock) => break lock,
            Err(ReconcileError::Unknown) if std::time::Instant::now() < lock_deadline => {
                std::thread::sleep(Duration::from_millis(20));
            }
            Err(error) => return Err(error),
        }
    };
    let rows = crate::reconcile_store::inventory(root)?;
    let mut validated = Vec::new();
    slots::with_table(root, Duration::from_secs(2), |table| {
        for row in &rows {
            for slot in table
                .active
                .iter()
                .filter(|slot| slot.bound && slot.run_id == row.run_id)
            {
                let identity = row.identity.as_ref().ok_or(ReconcileError::Unknown)?;
                if identity.pid != slot.pid
                    || identity.start_time != Some(slot.start_time)
                    || identity.group != slot.group
                    || serde_json::to_value(slot.role).map_err(|_| ReconcileError::Invalid)?
                        != row.role
                {
                    return Err(ReconcileError::Invalid);
                }
                validated.push((
                    slot.run_id.clone(),
                    slot.pid,
                    slot.start_time,
                    slot.group.clone(),
                ));
            }
        }
        Ok(())
    })
    .map_err(|_| ReconcileError::Io)??;
    // Replay uses daemon-level evidence configuration, never a record's role.
    let store = crate::evidence::EvidenceStore::open(root, crate::evidence::enabled(root, ""))
        .map_err(|_| ReconcileError::Io)?;
    replay(root, &store)?;
    let restart = now()?;
    let horizon = restart
        .checked_add(30 * 24 * 60 * 60 * 1000)
        .ok_or(ReconcileError::Invalid)?;
    let mut watched = Vec::new();
    let mut gone = Vec::new();
    for row in rows {
        if let Some(group) = row.identity.as_ref().and_then(|p| p.group.as_deref()) {
            crate::reconcile_backend::native::scope(&row.run_id, group)
                .map_err(|_| ReconcileError::Unknown)?;
        }
        if crate::reconcile_store::orphan_gone(root, &row)? {
            if let Some(identity) = row.identity {
                if let Some(start) = identity.start_time {
                    let key = (row.run_id, identity.pid, start, identity.group.clone());
                    if validated.contains(&key) && crate::reconcile_store::gone(Some(&identity))? {
                        gone.push(key);
                    }
                }
            }
            continue;
        }
        let leader = Group::open(&row.run_id, row.identity.ok_or(ReconcileError::Unknown)?)
            .map_err(|_| ReconcileError::Unknown)?;
        let dir = root.join("runs").join(&row.run_id);
        let _record_lock = crate::reconcile_store::lock(&dir, &dir.join("record.lock"))?;
        let mut record = RunRecord::read(root, &row.run_id).map_err(|_| ReconcileError::Io)?;
        let watch = Watch::new(
            serde_json::from_value(serde_json::json!(row.role))
                .map_err(|_| ReconcileError::Invalid)?,
            Limits::default(),
            restart,
            (row.deadline_ms.unwrap_or(horizon).min(horizon), Some(0)),
            StopPolicy {
                grace_ms: 10_000,
                interval_ms: 2_000,
            },
            record.get("first_activity").is_some_and(|v| !v.is_null()),
        );
        watch.attach(&mut record, row.attempt)?;
        watched.push((
            row.run_id,
            row.attempt,
            leader,
            watch,
            pg_worker_core::cpu::Sampler::new(1),
        ));
    }
    // No capability is issued on any earlier error, even after partial progress.
    gone.retain(|(id, _, _, _)| !watched.iter().any(|(run, _, _, _, _)| run == id));
    replay(root, &store)?;
    reclaim_after(
        root,
        Reconciled {
            root: root.into(),
            gone,
        },
        restart,
    )?;
    let root = root.to_path_buf();
    let stop = guard.stop.clone();
    guard.thread = Some(std::thread::spawn(move || {
        while !stop.load(Ordering::Acquire) {
            if let Ok(now) = now() {
                watched.retain_mut(|(run, attempt, child, watch, sampler)| {
                    if let Some(signal) = sampler.poll(now, &mut || child.cpu()) {
                        watch.cpu(signal);
                    }
                    let dir = root.join("runs").join(&*run);
                    let Ok(_record_lock) =
                        crate::reconcile_store::lock(&dir, &dir.join("record.lock"))
                    else {
                        return true;
                    };
                    let Ok(mut record) = RunRecord::read(&root, run) else {
                        return true;
                    };
                    !matches!(
                        watch.advance(child, &root, &mut record, *attempt, now),
                        Ok(Step::Ended(_))
                    )
                });
            }
            std::thread::sleep(Duration::from_millis(50));
        }
    }));
    Ok(())
}
#[cfg(not(target_os = "linux"))]
fn start(_: &Path, _: &mut Guard) -> Result<(), ReconcileError> {
    Err(ReconcileError::Unknown)
}
