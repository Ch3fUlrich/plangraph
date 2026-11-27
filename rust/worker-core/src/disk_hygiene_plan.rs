//! Device-scoped disk hygiene reclamation and its pressure outcomes.
use super::{display_path, HygieneReport, KeptPath};
use crate::host_link::{DISK_HYGIENE_BELOW_ALARM, DISK_HYGIENE_ZERO_PROGRESS};
pub use crate::shared_target::Candidate;
use discovery::rank_slices;
pub use discovery::{FinalProof, Layout};
#[path = "disk_hygiene_plan_discovery.rs"]
mod discovery;
use crate::cargo_pool_trim::ArtifactWalk;
use serde_json::{json, Value};
use std::{
    collections::BTreeSet,
    fs, io,
    path::{Path, PathBuf},
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
    rank_slices(
        &mut rows,
        budget,
        budget.start + budget.rank_for,
        false,
        None,
    );
    rows
}

/// Continue incomplete measurements within the refresh budget and the overall pass deadline.
pub fn refresh_ranking(rows: &mut [(Candidate, Ranked)], budget: &Budget<'_>) {
    refresh_with_lease(rows, budget, None);
}

fn refresh_with_lease(rows: &mut [(Candidate, Ranked)], budget: &Budget<'_>, held: Option<&Path>) {
    for (_, row) in &mut *rows {
        row.work = 0;
        row.spent = Duration::ZERO;
    }
    let end = (budget.start + budget.pass_for).min((budget.clock)() + budget.refresh_for);
    rank_slices(rows, budget, end, true, held);
}

impl HygieneReport {
    pub(crate) fn pressure_events(&mut self) -> Vec<Value> {
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
}

/// Fresh protection, process and space samples are supplied by the invocation.
pub struct ReclaimChecks<'a> {
    pub protection: &'a dyn Fn(&Candidate, Instant) -> io::Result<Option<&'static str>>,
    pub inventory: &'a dyn Fn(Instant) -> io::Result<super::HygieneInventory>,
    pub space: &'a dyn Fn(&Path) -> io::Result<u64>,
}

/// Reclaim a ranked set with one final lease and a fresh free-space sample after each removal.
pub fn reclaim(
    report: &mut HygieneReport,
    candidates: Vec<Candidate>,
    budget: &Budget<'_>,
    checks: &ReclaimChecks<'_>,
) -> bool {
    // SAFETY: geteuid takes no arguments.
    let uid = unsafe { libc::geteuid() };
    let now = budget.clock;
    let ranking_end = budget.start + budget.rank_for;
    let begin = now();
    let initial = (checks.inventory)(ranking_end);
    report
        .stage_durations
        .insert("inventory", now().duration_since(begin).as_secs_f64());
    let mut usable = initial.is_ok();
    let mut seen = BTreeSet::new();
    let mut rows: Vec<_> = candidates
        .into_iter()
        .filter(|item| seen.insert(item.path.clone()))
        .map(|item| {
            let reason =
                match &initial {
                    Ok(inv) => item.safe(uid).err().or_else(|| {
                        match (checks.protection)(&item, ranking_end) {
                            Ok(reason) => {
                                reason.or_else(|| inv.blocks(&item.scope).then_some("busy"))
                            }
                            Err(error) => {
                                report
                                    .summary
                                    .error
                                    .get_or_insert_with(|| error.to_string());
                                usable = false;
                                Some("probe_unavailable")
                            }
                        }
                    }),
                    Err(_) => Some("probe_unavailable"),
                };
            (
                item,
                Ranked {
                    reason,
                    ..Ranked::default()
                },
            )
        })
        .collect();
    let apply = !report.summary.dry_run;
    let mut proof: Option<FinalProof> = None;
    let mut free: std::collections::BTreeMap<_, _> = report
        .devices
        .iter()
        .map(|(&device, row)| (device, row.start_free_bytes))
        .collect();
    let mut scanned = Duration::ZERO;
    let mut attempted = Duration::ZERO;
    let mut removed = BTreeSet::new();
    for (first, maintenance) in [(true, false), (false, false), (false, true)] {
        if !usable {
            break;
        }
        let tick = now();
        if !first {
            let end = (tick + budget.refresh_for).min(budget.start + budget.pass_for);
            let variants: Vec<_> = rows
                .iter()
                .filter(|(item, row)| {
                    item.cap.is_some()
                        && !item.variant
                        && matches!(row.reason, Some("young" | "unsafe" | "unrecognized"))
                        && proof
                            .as_ref()
                            .is_none_or(|held| held.item.lease_root != item.lease_root)
                })
                .flat_map(|(item, _)| crate::cargo_pool::hygiene_variants(item, || now() < end))
                .filter(|item| seen.insert(item.path.clone()))
                .map(|item| (item, Ranked::default()))
                .collect();
            rows.extend(variants);
        }
        if first {
            rank_slices(&mut rows, budget, ranking_end, false, None);
        } else if !maintenance {
            refresh_with_lease(
                &mut rows,
                budget,
                proof.as_ref().map(|held| held.item.lease_root.as_path()),
            );
        }
        scanned += now().duration_since(tick);
        if let Some(held) = &proof {
            if let Some(i) = rows
                .iter()
                .position(|(item, _)| item.path == held.item.path)
            {
                rows.swap(0, i);
            }
        }
        let end = budget.start
            + if first {
                budget.first_for
            } else {
                budget.pass_for
            };
        let tick = now();
        for idx in 0..rows.len() {
            if now() >= end {
                break;
            }
            let (item, row) = &mut rows[idx];
            if row.reason.is_some() || removed.contains(&item.path) {
                continue;
            }
            let id = item.identity.0;
            let Some(device) = report.devices.get_mut(&id) else {
                row.reason = Some("root_unavailable");
                continue;
            };
            let Some(available) = free[&id] else {
                row.reason = Some("probe_unavailable");
                continue;
            };
            let hot = device
                .start_free_bytes
                .is_some_and(|start| start < device.cleanup_floor_bytes);
            if available >= device.cleanup_floor_bytes {
                if hot || !item.maintenance {
                    row.reason = Some("floor_met");
                    continue;
                }
                if !maintenance {
                    continue;
                }
                row.reason =
                    crate::cargo_pool::hygiene_cap_failure(item, &report.summary.projected, || {
                        now() < end
                    });
                if row.reason.is_some() {
                    continue;
                }
            }
            if removed.iter().any(|path| item.path.starts_with(path)) {
                row.reason = Some("covered");
                continue;
            }
            let tested = (|| {
                if proof.is_none() {
                    proof = Some(FinalProof::begin(item.clone())?);
                }
                proof.as_mut().unwrap().advance(|| now() < end)
            })();
            match tested {
                Ok(false) => break,
                Err(reason) => {
                    row.reason = Some(reason);
                    proof = None;
                    continue;
                }
                Ok(true) => (),
            }
            // Close the measurement stream before process inventory observes this invocation's fds.
            let scope = item.scope.clone();
            for (other, rank) in &mut rows {
                if other.path.starts_with(&scope) {
                    rank.walk.close_streams();
                }
            }
            let (item, row) = &mut rows[idx];
            let result = proof.as_ref().unwrap().remove(
                apply,
                || {
                    match (checks.protection)(item, end) {
                        Ok(None) => (),
                        Ok(reason) => {
                            row.reason = reason;
                            return Ok(false);
                        }
                        Err(error) => {
                            report
                                .summary
                                .error
                                .get_or_insert_with(|| error.to_string());
                            usable = false;
                            return Ok(false);
                        }
                    }
                    if now() >= end {
                        return Ok(false);
                    }
                    let Ok(inventory) = (checks.inventory)(end) else {
                        usable = false;
                        return Ok(false);
                    };
                    let mut lease = item.lease_root.as_os_str().to_os_string();
                    let suffix = [".plangraph-lock", "/.lease"];
                    lease.push(suffix[usize::from(item.selector_scratch)]);
                    if inventory.blocks_except(&item.scope, Path::new(&lease)) {
                        row.reason = Some("busy");
                        return Ok(false);
                    }
                    Ok(true)
                },
                || {
                    let elapsed = now();
                    if elapsed >= end {
                        return false;
                    }
                    report
                        .time_to_first_removal
                        .get_or_insert(elapsed.duration_since(budget.start).as_secs_f64());
                    true
                },
            );
            match result {
                Ok(None) if !usable || now() >= end => break,
                Ok(None) => {
                    row.reason.get_or_insert("unsafe");
                }
                Err(_) => row.reason = Some("remove_failed"),
                Ok(Some(bytes)) => {
                    removed.insert(item.path.clone());
                    let kind = if item.cap.is_some() {
                        "pool_evict "
                    } else {
                        ""
                    };
                    report.summary.actions.push(format!(
                        "{} {kind}{} bytes={bytes}",
                        if apply { "removed" } else { "would-remove" },
                        display_path(&item.path)
                    ));
                    if apply {
                        device.freed_bytes = device.freed_bytes.saturating_add(bytes);
                        let sample = (checks.space)(&device.root).map_err(|error| {
                            report
                                .summary
                                .error
                                .get_or_insert_with(|| error.to_string());
                        });
                        free.insert(id, sample.ok());
                    } else {
                        device.projected_bytes +=
                            report.summary.projected.project(&item.path, bytes);
                        free.insert(
                            id,
                            device
                                .start_free_bytes
                                .map(|start| start.saturating_add(device.projected_bytes)),
                        );
                    }
                    if first {
                        proof = None;
                        break;
                    }
                }
            }
            proof = None;
        }
        attempted += now().duration_since(tick);
    }
    for (item, row) in rows {
        if removed.contains(&item.path) {
            continue;
        }
        let reason = if usable {
            row.reason.unwrap_or("run_budget")
        } else {
            "probe_unavailable"
        };
        report.budget_exhausted |= reason == "run_budget";
        report.kept.push(KeptPath {
            device: Some(item.identity.0),
            path: item.path,
            bytes: row.measured.then_some(row.walk.bytes),
            bytes_is_lower_bound: !row.walk.complete && row.measured,
            reason,
        });
    }
    report
        .stage_durations
        .insert("scanning", scanned.as_secs_f64());
    report
        .stage_durations
        .insert("reclamation", attempted.as_secs_f64());
    report.first_removal_deadline_missed = apply
        && report.devices.values().any(|row| {
            row.start_free_bytes
                .is_some_and(|free| free < row.cleanup_floor_bytes)
        })
        && report
            .time_to_first_removal
            .is_none_or(|elapsed| elapsed > budget.first_for.as_secs_f64());
    usable
}
