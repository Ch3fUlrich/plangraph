use serde::Deserialize;

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
            let key = format!(
                "host-disk: host={}",
                serde_json::to_string(&host.host).unwrap()
            );
            let top = consumers(host);
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
