//! Minute samples. Missing measurements remain null, never invented zeroes.
use serde::{Deserialize, Serialize};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    io::{self, BufRead, Write},
    path::Path,
};

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
pub struct Sample {
    pub host: String,
    pub ts: u64,
    pub cpu_load: Option<f64>,
    pub mem_avail_mb: Option<u64>,
    pub swap_used_mb: Option<u64>,
    pub psi_mem_avg10: Option<f64>,
    pub disk_pct: Option<f64>,
    pub cores: usize,
    pub runs: u64,
    pub ready: u64,
    pub sessions: BTreeSet<String>,
    pub sessions_lost: Vec<String>,
    pub oom_kill: Option<u64>,
    pub top_rss: Vec<Rss>,
    pub reachable: bool,
}
#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Rss {
    pub pid: u32,
    pub name: String,
    pub rss_mb: u64,
}

pub fn valid_host(host: &str) -> bool {
    !host.is_empty()
        && host.len() <= 128
        && host
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b))
        && host != "."
        && host != ".."
}
pub fn read(path: &Path) -> io::Result<Vec<Sample>> {
    let file = fs::File::open(path)?;
    io::BufReader::new(file)
        .lines()
        .map(|line| serde_json::from_str(&line?).map_err(io::Error::other))
        .collect()
}
pub fn annotate(now: &mut Sample, old: Option<&Sample>) {
    now.sessions_lost.clear();
    if let Some(old) = old.filter(|o| {
        o.host == now.host
            && o.reachable
            && now.reachable
            && now.ts > o.ts
            && now.ts - o.ts <= 90
            && o.oom_kill.zip(now.oom_kill).is_some_and(|(a, b)| b > a)
    }) {
        now.sessions_lost = old.sessions.difference(&now.sessions).cloned().collect();
    }
}
pub fn append(dir: &Path, sample: &mut Sample) -> io::Result<()> {
    if !valid_host(&sample.host) {
        return Err(io::Error::other("invalid host id"));
    }
    fs::create_dir_all(dir)?;
    let path = dir.join(format!("{}.ndjson", sample.host));
    let old = match read(&path) {
        Ok(rows) => rows.last().cloned(),
        Err(e) if e.kind() == io::ErrorKind::NotFound => None,
        Err(e) => return Err(e),
    };
    annotate(sample, old.as_ref());
    let mut file = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)?;
    writeln!(file, "{}", serde_json::to_string(sample)?)
}

#[derive(Debug, Default, Serialize)]
pub struct Summary {
    pub runs_mean: f64,
    pub runs_p90: u64,
    pub idle_with_ready_hours: f64,
    pub oom_kill_delta: u64,
    pub sessions_lost: usize,
    pub unreachable_minutes: u64,
}
pub fn summarize(rows: &[Sample], start: u64, end: u64) -> BTreeMap<String, Summary> {
    let mut hosts: BTreeMap<String, BTreeMap<u64, &Sample>> = BTreeMap::new();
    for row in rows.iter().filter(|r| r.ts >= start && r.ts < end) {
        hosts
            .entry(row.host.clone())
            .or_default()
            .insert(row.ts / 60, row);
    }
    hosts
        .into_iter()
        .map(|(host, minutes)| {
            let mut report = Summary {
                unreachable_minutes: end.saturating_sub(start).div_ceil(60),
                ..Summary::default()
            };
            let mut counts = Vec::new();
            let mut previous: Option<u64> = None;
            for row in minutes.values().filter(|r| r.reachable) {
                report.unreachable_minutes = report.unreachable_minutes.saturating_sub(1);
                counts.push(row.runs);
                if row.runs == 0 && row.ready > 0 {
                    report.idle_with_ready_hours += 1.0 / 60.0;
                }
                if let Some(counter) = row.oom_kill {
                    report.oom_kill_delta += previous.map_or(0, |p| counter.saturating_sub(p));
                    previous = Some(counter);
                }
                report.sessions_lost += row.sessions_lost.len();
            }
            counts.sort_unstable();
            if !counts.is_empty() {
                report.runs_mean = counts.iter().sum::<u64>() as f64 / counts.len() as f64;
                report.runs_p90 = counts[(counts.len() * 9).div_ceil(10) - 1];
            }
            (host, report)
        })
        .collect()
}

/// Host-specific sampled counter delta; unknown when no baseline/end coverage exists.
pub fn run_delta(dir: &Path, host: &str, start: f64, end: f64) -> Option<u64> {
    if !valid_host(host) || !start.is_finite() || end < start {
        return None;
    }
    let mut rows = read(&dir.join(format!("{host}.ndjson"))).ok()?;
    rows.retain(|s| s.host == host && s.reachable && s.oom_kill.is_some() && s.ts as f64 <= end);
    rows.sort_by_key(|s| s.ts);
    let baseline = rows
        .iter()
        .rposition(|s| s.ts as f64 <= start && start - s.ts as f64 <= 120.0)?;
    let interval = &rows[baseline..];
    if end - interval.last()?.ts as f64 > 120.0 {
        return None;
    }
    Some(
        interval
            .windows(2)
            .map(|w| {
                w[1].oom_kill
                    .unwrap()
                    .saturating_sub(w[0].oom_kill.unwrap())
            })
            .sum(),
    )
}
