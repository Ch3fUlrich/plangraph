//! Device-scoped disk hygiene reclamation and its pressure outcomes.
use super::{hygiene_report::display_path, HygieneReport, KeptPath};
use crate::host_link::{
    enqueue_disk_hygiene, DISK_HYGIENE_BELOW_ALARM, DISK_HYGIENE_ZERO_PROGRESS,
};
use serde_json::{json, Value};
use std::{io, path::Path};

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
