use serde::Deserialize;
use std::path::Path;

/// A host's disk alert names this many of the largest directories (rule 9.23).
const TOP_CONSUMERS: usize = 5;

const DISK_USED_PERCENT: f64 = 85.0;
const CHECKOUT_BEHIND_COMMITS: u64 = 10;
const CHECKOUT_AGE_HOURS: f64 = 24.0;

#[derive(Deserialize)]
struct Consumer {
    path: String,
    bytes: u64,
}

#[derive(Deserialize)]
pub struct Host {
    host: String,
    tmp_used_percent: f64,
    root_used_percent: f64,
    #[serde(default)]
    work_used_percent: Vec<f64>,
    /// Collector-measured directories on the full filesystem, any order.
    #[serde(default)]
    top_consumers: Vec<Consumer>,
    checkout_head: String,
    origin_main_head: String,
    // Collector supplies the ancestry count, not a comparison of commit timestamps.
    behind_commits: u64,
    age_hours: f64,
}

/// Fill a local host entry that has no `top_consumers` from the directories `pg-disk-hygiene`
/// sampled at its 85% watermark (`disk-watermarks.json`, any filesystem). Collectors for other
/// hosts supply their own list.
pub fn attach_local_consumers(hosts: &mut [Host], local: &str, watermarks: &Path) {
    let Some(host) = hosts
        .iter_mut()
        .find(|h| h.host == local && h.top_consumers.is_empty())
    else {
        return;
    };
    let Ok(bytes) = std::fs::read(watermarks) else {
        return;
    };
    let Ok(alerts) = serde_json::from_slice::<serde_json::Value>(&bytes) else {
        return;
    };
    for alert in alerts.as_object().into_iter().flat_map(|a| a.values()) {
        for row in alert["directories"].as_array().into_iter().flatten() {
            if let (Some(path), Some(bytes)) = (row["path"].as_str(), row["bytes"].as_u64()) {
                host.top_consumers.push(Consumer {
                    path: path.to_owned(),
                    bytes,
                });
            }
        }
    }
}

/// The name this host goes by in the snapshot, then the hygiene state it reads.
pub fn local_name() -> Option<String> {
    std::env::var("PLANGRAPH_HOST_NAME")
        .ok()
        .filter(|name| !name.is_empty())
        .or_else(|| {
            std::fs::read_to_string("/proc/sys/kernel/hostname")
                .ok()
                .map(|name| name.trim().to_owned())
        })
}

pub fn watermarks_path() -> Option<std::path::PathBuf> {
    let var = |name| std::env::var_os(name).filter(|v| !v.is_empty());
    var("PLANGRAPH_DISK_WATERMARKS")
        .map(std::path::PathBuf::from)
        .or_else(|| {
            var("PLANGRAPH_SPAWN_STATE")
                .map(std::path::PathBuf::from)
                .or_else(|| {
                    var("HOME").map(|h| std::path::PathBuf::from(h).join(".local/state/plangraph"))
                })
                .map(|state| state.join("disk-watermarks.json"))
        })
}

fn consumers(host: &Host) -> String {
    let mut rows: Vec<&Consumer> = host.top_consumers.iter().collect();
    rows.sort_by(|a, b| b.bytes.cmp(&a.bytes).then_with(|| a.path.cmp(&b.path)));
    rows.iter()
        .take(TOP_CONSUMERS)
        .map(|row| format!("{}={}", row.path, row.bytes))
        .collect::<Vec<_>>()
        .join(" ")
}

/// Pairs of (dedupe key, message): the key is stable while the consumer sizes move.
pub fn messages(hosts: &[Host]) -> Vec<(String, String)> {
    let mut messages = Vec::new();
    for host in hosts {
        if [host.tmp_used_percent, host.root_used_percent]
            .into_iter()
            .chain(host.work_used_percent.iter().copied())
            .any(|percent| percent > DISK_USED_PERCENT)
        {
            let top = consumers(host);
            // The key changes once when the first measurement arrives, so an alert queued bare
            // (the snapshot came before hygiene sampled) is followed by one that names the
            // consumers; moving sizes never change it.
            let key = format!(
                "host-disk: host={}{}",
                serde_json::to_string(&host.host).unwrap(),
                if top.is_empty() { "" } else { " measured" }
            );
            let message = if top.is_empty() {
                key.clone()
            } else {
                format!("{key} top={top}")
            };
            messages.push((key, message));
        }
        if host.checkout_head != host.origin_main_head
            && host.behind_commits > 0
            && (host.behind_commits > CHECKOUT_BEHIND_COMMITS
                || host.age_hours > CHECKOUT_AGE_HOURS)
        {
            let key = format!(
                "host-code: host={}",
                serde_json::to_string(&host.host).unwrap()
            );
            messages.push((key.clone(), key));
        }
    }
    messages.sort();
    messages.dedup_by(|a, b| a.0 == b.0);
    messages
}
