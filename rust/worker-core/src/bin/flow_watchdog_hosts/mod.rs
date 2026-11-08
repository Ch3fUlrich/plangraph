use serde::Deserialize;

const DISK_USED_PERCENT: f64 = 85.0;
const CHECKOUT_BEHIND_COMMITS: u64 = 10;
const CHECKOUT_AGE_HOURS: f64 = 24.0;

#[derive(Deserialize)]
pub struct Host {
    host: String,
    tmp_used_percent: f64,
    root_used_percent: f64,
    checkout_head: String,
    origin_main_head: String,
    // Collector supplies the ancestry count, not a comparison of commit timestamps.
    behind_commits: u64,
    age_hours: f64,
}

pub fn messages(hosts: &[Host]) -> Vec<String> {
    let mut messages = Vec::new();
    for host in hosts {
        if host.tmp_used_percent > DISK_USED_PERCENT || host.root_used_percent > DISK_USED_PERCENT {
            messages.push(format!(
                "host-disk: host={} /tmp={}%; /={}%; threshold={}%",
                host.host, host.tmp_used_percent, host.root_used_percent, DISK_USED_PERCENT
            ));
        }
        if host.checkout_head != host.origin_main_head
            && host.behind_commits > 0
            && (host.behind_commits > CHECKOUT_BEHIND_COMMITS
                || host.age_hours > CHECKOUT_AGE_HOURS)
        {
            messages.push(format!(
                "host-code: host={} checkout={} origin/main={} behind={} age_hours={}",
                host.host,
                host.checkout_head,
                host.origin_main_head,
                host.behind_commits,
                host.age_hours
            ));
        }
    }
    messages.sort();
    messages
}
