use serde::Deserialize;
use std::path::Path;

mod disks;
mod key;
mod local;
mod names;
mod transitions;

pub use disks::Row;
pub use names::Names;
pub use transitions::{canonical, migrate, queued, reconcile, recovered, settle, transition};

const DISK_USED_PERCENT: f64 = 85.0;
const CHECKOUT_BEHIND_COMMITS: u64 = 10;
const CHECKOUT_AGE_HOURS: f64 = 24.0;

#[derive(Deserialize)]
pub struct Host {
    host: String,
    /// Legacy percentages; a row that lists `disks` needs neither.
    #[serde(default)]
    tmp_used_percent: Option<f64>,
    #[serde(default)]
    root_used_percent: Option<f64>,
    #[serde(default)]
    work_used_percent: Option<Vec<f64>>,
    /// Keyed samples; when present they are the whole disk observation of the row.
    #[serde(default)]
    disks: Option<Vec<disks::Sample>>,
    checkout_head: String,
    origin_main_head: String,
    // Collector supplies the ancestry count, not a comparison of commit timestamps.
    behind_commits: u64,
    age_hours: f64,
}

impl Host {
    /// A row names its disks either by `disks` or by the legacy root and temp percentages.
    pub fn validate(&self) -> Result<(), String> {
        if self.disks.is_none()
            && (self.root_used_percent.is_none() || self.tmp_used_percent.is_none())
        {
            return Err(format!(
                "host {}: needs disks or both root_used_percent and tmp_used_percent",
                self.host
            ));
        }
        Ok(())
    }
}

/// The disk observation of every snapshot row, in row order.
pub fn observe(hosts: &[Host], names: &Names, state: &Path) -> Vec<Row> {
    hosts
        .iter()
        .map(|host| disks::row(host, names, state))
        .collect()
}

/// Whether `line` belongs to a host alert (kept as a whole when the snapshot is unusable).
pub fn is_host_line(line: &str) -> bool {
    key::Key::parse(line).is_some()
}

/// An `active` line of a host alert that this snapshot does not settle: a missing sample is not a
/// recovery. A host that has no row keeps all its lines. A filesystem with no sample in its host's
/// row keeps its line. An aggregate line, which names no filesystem, ends only when the row has no
/// hot filesystem at all.
pub fn retained(line: &str, rows: &[Row]) -> bool {
    let Some(key) = key::Key::parse(line) else {
        return false;
    };
    let Some(row) = rows.iter().find(|row| row.host == key.host) else {
        return true;
    };
    match (key.disk, key.fs) {
        (false, _) => false,
        (true, Some(device)) => !row.groups.contains_key(&device),
        (true, None) => row.any_hot() && !row.anonymous_hot,
    }
}

/// Pairs of (`active` key, message). A hot filesystem queues its measured alert once, optionally
/// after a bare one; the measured key then stays for the whole crossing, so a measurement that
/// comes and goes queues nothing more. Samples that name no filesystem share one aggregate alert.
pub fn messages(hosts: &[Host], rows: &[Row], previous_active: &str) -> Vec<(String, String)> {
    let mut messages = Vec::new();
    for (host, row) in hosts.iter().zip(rows) {
        for (device, group) in row.groups.iter().filter(|(_, group)| group.hot()) {
            let measured = key::Key::disk(&row.host, Some(device), true).render();
            if !group.consumers.is_empty() {
                messages.push((measured.clone(), format!("{measured} top={}", group.top())));
            } else if previous_active.lines().any(|line| line == measured) {
                messages.push((measured.clone(), measured));
            } else {
                let bare = key::Key::disk(&row.host, Some(device), false).render();
                messages.push((bare.clone(), bare));
            }
        }
        if row.anonymous_hot {
            let bare = key::Key::disk(&row.host, None, false).render();
            messages.push((bare.clone(), bare));
        }
        if host.checkout_head != host.origin_main_head
            && host.behind_commits > 0
            && (host.behind_commits > CHECKOUT_BEHIND_COMMITS
                || host.age_hours > CHECKOUT_AGE_HOURS)
        {
            let code = key::Key::code(&row.host).render();
            messages.push((code.clone(), code));
        }
    }
    messages.sort();
    messages.dedup_by(|a, b| a.0 == b.0);
    messages
}

/// Record the filesystems the watchdog saw recover, before `active` forgets them.
pub fn record_recoveries(state: &Path, crossings: &[key::Key]) -> std::io::Result<()> {
    local::record_recoveries(state, crossings, local::now())
}
