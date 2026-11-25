//! One host's disk samples grouped by filesystem. A sample names its filesystem by `device`
//! (on Unix the `st_dev` of the sampled path, as a decimal string; on Windows a volume id the
//! collector chooses). A legacy percentage names none, except the local root (and the local
//! temp path when `PLANGRAPH_HOST_TMP_PATH` names the path both sides sample): those get the
//! local `st_dev`. Everything else is anonymous and can only raise the bare aggregate alert.
use super::{names::Names, Host, DISK_USED_PERCENT};
use serde::Deserialize;
use std::{collections::BTreeMap, path::Path};

/// A host's disk alert names this many of the largest directories (rule 9.23).
pub const TOP_CONSUMERS: usize = 5;

#[derive(Clone, Deserialize)]
pub struct Consumer {
    path: String,
    bytes: u64,
}

/// A collector-measured filesystem: usage and the directories on that filesystem together.
#[derive(Deserialize)]
pub struct Sample {
    device: String,
    used_percent: f64,
    #[serde(default)]
    consumers: Vec<Consumer>,
}

pub struct Group {
    pub percent: f64,
    pub consumers: Vec<Consumer>,
}

impl Group {
    pub fn hot(&self) -> bool {
        self.percent > DISK_USED_PERCENT
    }

    /// The largest directories, any input order, each path once.
    pub fn top(&self) -> String {
        let mut rows = self.consumers.clone();
        rows.sort_by(|a, b| b.bytes.cmp(&a.bytes).then_with(|| a.path.cmp(&b.path)));
        rows.dedup_by(|later, kept| later.path == kept.path);
        rows.iter()
            .take(TOP_CONSUMERS)
            .map(|row| format!("{}={}", row.path, row.bytes))
            .collect::<Vec<_>>()
            .join(" ")
    }
}

/// What one snapshot row says about its host's disks, keyed by the host's canonical id.
pub struct Row {
    pub host: String,
    /// Sampled filesystems; a device missing here is unknown, not recovered.
    pub groups: BTreeMap<String, Group>,
    pub anonymous_hot: bool,
}

impl Row {
    pub fn any_hot(&self) -> bool {
        self.anonymous_hot || self.groups.values().any(Group::hot)
    }

    fn add(&mut self, device: Option<String>, percent: f64, consumers: Vec<Consumer>) {
        let Some(device) = device else {
            self.anonymous_hot |= percent > DISK_USED_PERCENT;
            return;
        };
        let group = self.groups.entry(device).or_insert(Group {
            percent,
            consumers: vec![],
        });
        group.percent = group.percent.max(percent);
        group.consumers.extend(consumers);
    }
}

#[cfg(unix)]
pub fn device_of(path: &Path) -> Option<String> {
    use std::os::unix::fs::MetadataExt;
    std::fs::metadata(path)
        .ok()
        .map(|meta| meta.dev().to_string())
}

#[cfg(not(unix))]
pub fn device_of(_: &Path) -> Option<String> {
    None
}

pub fn row(host: &Host, names: &Names, state: &Path) -> Row {
    let canonical = names.canonical(&host.host);
    let local = names.is_local(&canonical);
    let mut row = Row {
        host: canonical,
        groups: BTreeMap::new(),
        anonymous_hot: false,
    };
    let local_device = |path: &Path| local.then(|| device_of(path)).flatten();
    if let Some(samples) = &host.disks {
        // A present keyed list is authoritative; the legacy fields are not read beside it.
        for sample in samples {
            row.add(
                Some(sample.device.clone()),
                sample.used_percent,
                sample.consumers.clone(),
            );
        }
    } else {
        if let Some(percent) = host.root_used_percent {
            row.add(local_device(Path::new("/")), percent, vec![]);
        }
        if let Some(percent) = host.tmp_used_percent {
            let temp = std::env::var_os("PLANGRAPH_HOST_TMP_PATH").filter(|v| !v.is_empty());
            row.add(
                temp.and_then(|path| local_device(Path::new(&path))),
                percent,
                vec![],
            );
        }
        for percent in host.work_used_percent.iter().flatten() {
            row.add(None, *percent, vec![]);
        }
    }
    if local {
        attach_local(&mut row, state);
    }
    row
}

/// Name the directories `pg-disk-hygiene` measured at its watermark for each hot local filesystem
/// the collector gave none for. The watermark file is keyed by device; a row older than the
/// watchdog's last observed recovery of that filesystem belongs to an earlier crossing.
#[cfg(unix)]
fn attach_local(row: &mut Row, state: &Path) {
    use super::key::Key;
    let Some(marks) = super::local::watermarks() else {
        return;
    };
    let recoveries = super::local::recoveries(state);
    let host = row.host.clone();
    for (device, group) in &mut row.groups {
        if !group.hot() || !group.consumers.is_empty() {
            continue;
        }
        let Some(entry) = marks.get(device) else {
            continue;
        };
        let recovered = recoveries
            .get(&Key::disk(&host, Some(device), false).render())
            .copied();
        let fresh = match (entry["measured_at"].as_u64(), recovered) {
            (_, None) => true,
            (Some(at), Some(recovered)) => at > recovered,
            (None, Some(_)) => false,
        };
        if !fresh {
            continue;
        }
        group.consumers = entry["directories"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(|row| {
                Some(Consumer {
                    path: row["path"].as_str()?.to_owned(),
                    bytes: row["bytes"].as_u64()?,
                })
            })
            .collect();
    }
}

#[cfg(not(unix))]
fn attach_local(_: &mut Row, _: &Path) {}
