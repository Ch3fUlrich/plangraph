//! Device-scoped disk hygiene reclamation and its pressure outcomes.
use super::{hygiene_report::display_path, HygieneReport, KeptPath};
use crate::host_link::{
    enqueue_disk_hygiene, DISK_HYGIENE_BELOW_ALARM, DISK_HYGIENE_ZERO_PROGRESS,
};
pub use crate::shared_target::Candidate;
use crate::{
    cargo_cache::ExclusiveLease,
    cargo_pool_trim::{remove_guarded_if, stamp, ArtifactWalk},
};
use serde_json::{json, Value};
use std::{
    collections::BTreeSet,
    fs, io,
    path::Path,
    time::{Duration, Instant},
};

/// One monotonic clock and limits for discovery, ranking, and reclamation.
pub struct Budget<'a> {
    pub start: Instant,
    pub clock: &'a dyn Fn() -> Instant,
    pub rank_for: Duration,
    pub first_for: Duration,
    pub refresh_for: Duration,
    pub pass_for: Duration,
    pub candidate_for: Duration,
    pub slice_entries: usize,
    pub candidate_entries: usize,
    pub rank_entries: usize,
    pub discovery_entries: usize,
}

impl<'a> Budget<'a> {
    pub fn new(start: Instant, clock: &'a dyn Fn() -> Instant) -> Self {
        Self {
            start,
            clock,
            rank_for: Duration::from_secs(45),
            first_for: Duration::from_secs(60),
            refresh_for: Duration::from_secs(15),
            pass_for: Duration::from_secs(300),
            candidate_for: Duration::from_secs(2),
            slice_entries: 1024,
            candidate_entries: 250_000,
            rank_entries: 1_000_000,
            discovery_entries: 16_384,
        }
    }
}

/// A candidate's freshness and ownership walk, held under its removal lease.
pub struct FinalProof {
    item: Candidate,
    uid: u32,
    _lease: ExclusiveLease,
    walk: ArtifactWalk,
}

impl FinalProof {
    /// Acquire the nonblocking owner lease before inspecting any candidate contents.
    pub fn begin(item: Candidate) -> Result<Self, &'static str> {
        // SAFETY: geteuid has no preconditions.
        let uid = unsafe { libc::geteuid() };
        item.safe(uid)?;
        let lease = item.lease()?;
        Ok(Self {
            item,
            uid,
            _lease: lease,
            walk: ArtifactWalk::default(),
        })
    }

    /// Continue until EOF or the current deadline, restarting stale observations under the lease.
    pub fn advance(&mut self, room: impl Fn() -> bool) -> Result<bool, &'static str> {
        self.item.safe(self.uid)?;
        match self.walk.unchanged(&room) {
            Some(false) => self.walk = ArtifactWalk::default(),
            Some(true) => (),
            None => return Ok(false),
        }
        while room() && !self.walk.complete {
            self.walk.step(
                &self.item.path,
                self.item.cutoff,
                self.item.identity.0,
                self.uid,
            )?;
        }
        Ok(self.walk.complete)
    }

    /// Apply a completed proof after refreshing protection. Return bytes only for an allowed action.
    pub fn remove(
        &self,
        apply: bool,
        allowed: impl FnOnce() -> io::Result<bool>,
        before_unlink: impl FnOnce() -> bool,
    ) -> io::Result<Option<u64>> {
        if !self.walk.complete {
            return Err(io::Error::other("candidate proof incomplete"));
        }
        self.item.safe(self.uid).map_err(io::Error::other)?;
        let mut authorized = Ok(false);
        let removed = remove_guarded_if(&self.item.boundary, &self.item.path, apply, |path| {
            authorized = allowed();
            matches!(authorized, Ok(true))
                && fs::symlink_metadata(path).ok().map(|meta| stamp(&meta))
                    == self.walk.root_stamp()
                && (!apply || before_unlink())
        })?;
        authorized?;
        Ok(removed.then_some(self.walk.credit))
    }
}

#[derive(Default)]
pub struct Ranked {
    measured: bool,
    walk: ArtifactWalk,
    pub reason: Option<&'static str>,
    work: usize,
    spent: Duration,
}

impl Ranked {
    pub fn estimate(&self) -> Option<u64> {
        self.measured.then_some(self.walk.bytes)
    }

    pub fn complete(&self) -> bool {
        self.walk.complete
    }

    pub fn reclaimable_bytes(&self) -> Option<u64> {
        self.walk.complete.then_some(self.walk.credit)
    }
}

fn rank_candidate(
    row: &mut Ranked,
    item: &Candidate,
    budget: &Budget<'_>,
    uid: u32,
    deadline: Instant,
    left: &mut usize,
    resuming: bool,
) {
    let tick = (budget.clock)();
    let end = deadline.min(tick + budget.candidate_for.saturating_sub(row.spent));
    let result = (|| {
        item.safe(uid)?;
        let _lease = item.lease()?;
        if let Some(observed) = row.walk.root_stamp() {
            let current = fs::symlink_metadata(&item.path).map_err(|_| "probe_unavailable")?;
            if stamp(&current) != observed {
                row.walk = ArtifactWalk::default();
            }
        }
        if resuming {
            match row.walk.unchanged(|| (budget.clock)() < end) {
                Some(false) => row.walk = ArtifactWalk::default(),
                Some(true) => (),
                None => {
                    row.spent = budget.candidate_for;
                    return Ok(());
                }
            }
        }
        let count = budget
            .slice_entries
            .min(budget.candidate_entries - row.work)
            .min(*left);
        for _ in 0..count {
            if row.walk.complete || (budget.clock)() >= end {
                break;
            }
            row.measured = true;
            row.work += 1;
            *left -= 1;
            row.walk
                .step(&item.path, item.cutoff, item.identity.0, uid)?;
        }
        Ok(())
    })();
    row.reason = result.err();
    row.spent += (budget.clock)().saturating_duration_since(tick);
}

/// Rank provider-approved targets in round-robin slices, holding only the current target's lease.
pub fn rank_candidates(
    candidates: Vec<Candidate>,
    budget: &Budget<'_>,
) -> Vec<(Candidate, Ranked)> {
    let mut seen = BTreeSet::new();
    let mut rows: Vec<_> = candidates
        .into_iter()
        .filter(|item| seen.insert((item.path.clone(), item.identity)))
        .map(|item| (item, Ranked::default()))
        .collect();
    rank_slices(&mut rows, budget, budget.start + budget.rank_for, false);
    rows
}

/// Continue incomplete measurements within the refresh budget and the overall pass deadline.
pub fn refresh_ranking(rows: &mut [(Candidate, Ranked)], budget: &Budget<'_>) {
    for (_, row) in &mut *rows {
        row.work = 0;
        row.spent = Duration::ZERO;
    }
    let end = (budget.start + budget.pass_for).min((budget.clock)() + budget.refresh_for);
    rank_slices(rows, budget, end, true);
}

fn rank_slices(rows: &mut [(Candidate, Ranked)], budget: &Budget<'_>, end: Instant, refresh: bool) {
    // SAFETY: geteuid has no preconditions.
    let uid = unsafe { libc::geteuid() };
    let mut left = budget.rank_entries;
    loop {
        let before = left;
        for (item, row) in &mut *rows {
            if left == 0 || (budget.clock)() >= end {
                break;
            }
            if row.reason.is_none()
                && !row.walk.complete
                && row.work < budget.candidate_entries
                && row.spent < budget.candidate_for
            {
                rank_candidate(
                    row,
                    item,
                    budget,
                    uid,
                    end,
                    &mut left,
                    refresh && row.work == 0,
                );
            }
        }
        if left == before {
            break;
        }
    }
    rows.sort_by(|(a, ra), (b, rb)| {
        rb.walk
            .bytes
            .cmp(&ra.walk.bytes)
            .then_with(|| a.path.cmp(&b.path))
    });
}

impl HygieneReport {
    fn pressure_events(&mut self) -> Vec<Value> {
        let mut alerts = vec![];
        for row in self.devices.values() {
            let Some(free) = row.end_free_bytes else {
                continue;
            };
            let below_cleanup = free < row.cleanup_floor_bytes;
            let below_alarm = free < row.alarm_floor_bytes;
            self.summary.alarm |= below_alarm;
            let rule = match (below_cleanup, below_alarm, row.freed_bytes) {
                (true, _, 0) => Some((DISK_HYGIENE_ZERO_PROGRESS, row.cleanup_floor_bytes)),
                (_, true, 1..) => Some((DISK_HYGIENE_BELOW_ALARM, row.alarm_floor_bytes)),
                _ => None,
            };
            let mut kept: Vec<_> = self
                .kept
                .iter()
                .filter(|path| path.device == Some(row.device))
                .collect();
            if kept.is_empty() && (below_cleanup || rule.is_some()) {
                self.kept.push(KeptPath {
                    device: Some(row.device),
                    path: row.root.clone(),
                    bytes: None,
                    bytes_is_lower_bound: false,
                    reason: "no_candidates",
                });
                kept = self.kept.last().into_iter().collect();
            }
            if self.summary.dry_run {
                continue;
            }
            if let Some((rule, floor)) = rule {
                self.summary.alarm = true;
                if rule == DISK_HYGIENE_ZERO_PROGRESS {
                    self.summary
                        .error
                        .get_or_insert_with(|| "disk hygiene zero progress".into());
                }
                kept.sort_by(|a, b| b.bytes.cmp(&a.bytes).then_with(|| a.path.cmp(&b.path)));
                kept.truncate(5);
                alerts.push(json!({"rule": rule, "device": row.device, "end_free_bytes": free,
                    "floor_bytes": floor, "freed_bytes": row.freed_bytes, "observed_at": row.observed_at, "kept": kept}));
            }
        }
        alerts
    }

    fn json(&self) -> io::Result<Value> {
        let free = self
            .devices
            .values()
            .filter_map(|row| row.end_free_bytes)
            .min();
        let mut report = serde_json::to_value(&self.summary)?;
        report["free_bytes"] = json!(free);
        report["devices"] = json!(self.devices.values().collect::<Vec<_>>());
        report["kept"] = json!(self.kept);
        report["started_at"] = json!(self.started_at.as_secs());
        report["time_to_first_removal"] = json!(self.time_to_first_removal);
        report["first_removal_deadline_missed"] = json!(self.first_removal_deadline_missed);
        report["stage_durations"] = json!(self.stage_durations);
        report["budget_exhausted"] = json!(self.budget_exhausted);
        Ok(report)
    }

    /// Finalize the report once. Report writes and alert enqueues have independent results.
    pub fn persist(
        &mut self,
        state: &Path,
        host: impl FnOnce() -> io::Result<String>,
    ) -> (io::Result<Vec<u8>>, io::Result<()>) {
        let alerts = self.pressure_events();
        self.summary.freed_bytes = self
            .devices
            .values()
            .map(|row| row.freed_bytes)
            .fold(0, u64::saturating_add);
        self.summary.skipped.extend(
            self.kept
                .iter()
                .map(|row| format!("{}: {}", display_path(&row.path), row.reason)),
        );
        let report = self
            .json()
            .and_then(|row| crate::disk_runtime::write_hygiene_report(state, &row));
        let queued = (|| {
            if alerts.is_empty() {
                return Ok(());
            }
            let host = host()?;
            let mut errors = vec![];
            for event in alerts {
                let device = event["device"].as_u64().unwrap();
                if let Err(error) = enqueue_disk_hygiene(state, self.started_at, &host, event) {
                    errors.push(format!("device {device}: {error}"));
                }
            }
            if errors.is_empty() {
                Ok(())
            } else {
                Err(io::Error::other(errors.join(", ")))
            }
        })();
        (report, queued)
    }
}
