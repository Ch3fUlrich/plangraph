//! The stage timeline of a batch or train from writer done to merge, and the report that names the
//! bottleneck stage per train and over the last N trains. The ledger is append-only JSON lines keyed by
//! subject (batch or train) and head; the keeper table is a separate step (see docs/landing-train.md).
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::BTreeMap;
use std::io::Write;
use std::path::Path;

/// One stage run: start, end (epoch seconds), host and result.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Stage {
    pub subject: String,
    pub head: String,
    /// `fast_checks`, `push_gate:<stage>`, `ci:<job>`, `review`, `go_request`, `merge`, `host_sync`.
    pub stage: String,
    pub host: String,
    pub start: i64,
    pub end: i64,
    pub result: String,
}

impl Stage {
    pub fn secs(&self) -> i64 {
        (self.end - self.start).max(0)
    }
}

pub fn append(path: &Path, stage: &Stage) -> Result<(), String> {
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir).map_err(|e| e.to_string())?;
    }
    let mut f = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
        .map_err(|e| e.to_string())?;
    let mut line = serde_json::to_vec(stage).map_err(|e| e.to_string())?;
    line.push(b'\n');
    f.write_all(&line).map_err(|e| e.to_string())
}

pub fn load(path: &Path) -> Result<Vec<Stage>, String> {
    match std::fs::read_to_string(path) {
        Ok(raw) => raw
            .lines()
            .filter(|l| !l.trim().is_empty())
            .map(|l| serde_json::from_str(l).map_err(|e| format!("invalid stage row: {e}")))
            .collect(),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(vec![]),
        Err(e) => Err(e.to_string()),
    }
}

fn epoch(v: &Value, field: &str) -> Option<i64> {
    chrono::DateTime::parse_from_rfc3339(v[field].as_str()?)
        .ok()
        .map(|t| t.timestamp())
}

/// One CI stage per job of a run, from GitHub's job list (`started_at`, `completed_at`, runner).
pub fn ci_stages(subject: &str, head: &str, jobs: &[Value]) -> Vec<Stage> {
    jobs.iter()
        .filter_map(|j| {
            Some(Stage {
                subject: subject.into(),
                head: head.into(),
                stage: format!("ci:{}", j["name"].as_str()?),
                host: j["runner_name"].as_str().unwrap_or("github").into(),
                start: epoch(j, "started_at")?,
                end: epoch(j, "completed_at")?,
                result: j["conclusion"].as_str().unwrap_or("unknown").into(),
            })
        })
        .collect()
}

/// The critical path: from the stage that ended last, walk back through the latest-ending stage that
/// finished before the current one started. Overlapping (parallel) stages are not on the path twice.
pub fn critical_path(stages: &[Stage]) -> Vec<Stage> {
    let mut path: Vec<Stage> = Vec::new();
    let Some(mut cur) = stages.iter().max_by_key(|s| (s.end, s.start)).cloned() else {
        return path;
    };
    loop {
        path.push(cur.clone());
        match stages
            .iter()
            .filter(|s| s.end <= cur.start && !path.contains(s))
            .max_by_key(|s| (s.end, s.start))
        {
            Some(prev) => cur = prev.clone(),
            None => break,
        }
    }
    path.reverse();
    path
}

#[derive(Debug, PartialEq, Eq)]
pub struct TrainReport {
    pub subject: String,
    /// Writer done to merge, wall clock.
    pub wall_secs: i64,
    pub critical_path: Vec<String>,
    /// The longest stage on the critical path, with its seconds.
    pub bottleneck: (String, i64),
    /// Time on the critical path spent between stages (waiting for a person or a queue).
    pub idle_secs: i64,
}

#[derive(Debug, PartialEq, Eq)]
pub struct Report {
    pub trains: Vec<TrainReport>,
    /// Critical-path seconds per stage name over the last N trains, largest first.
    pub top_stages: Vec<(String, i64)>,
}

/// Stage name without its job suffix is kept whole: `ci:rust` and `ci:browser` are different stages.
pub fn report(stages: &[Stage], last_n: usize) -> Report {
    let mut by: BTreeMap<&str, Vec<Stage>> = BTreeMap::new();
    for s in stages {
        by.entry(s.subject.as_str()).or_default().push(s.clone());
    }
    let mut order: Vec<(&str, i64)> = by
        .iter()
        .map(|(k, v)| (*k, v.iter().map(|s| s.end).max().unwrap_or(0)))
        .collect();
    order.sort_by_key(|(_, end)| std::cmp::Reverse(*end));
    order.truncate(last_n);
    let mut trains = Vec::new();
    let mut totals: BTreeMap<String, i64> = BTreeMap::new();
    for (subject, _) in order {
        let all = &by[subject];
        let path = critical_path(all);
        let busy: i64 = path.iter().map(Stage::secs).sum();
        let first = all.iter().map(|s| s.start).min().unwrap_or(0);
        let last = all.iter().map(|s| s.end).max().unwrap_or(0);
        let slowest = path.iter().max_by_key(|s| s.secs());
        for s in &path {
            *totals.entry(s.stage.clone()).or_insert(0) += s.secs();
        }
        trains.push(TrainReport {
            subject: subject.to_owned(),
            wall_secs: last - first,
            critical_path: path.iter().map(|s| s.stage.clone()).collect(),
            bottleneck: slowest.map_or((String::new(), 0), |s| (s.stage.clone(), s.secs())),
            idle_secs: (last - first - busy).max(0),
        });
    }
    let mut top_stages: Vec<(String, i64)> = totals.into_iter().collect();
    top_stages.sort_by(|a, b| b.1.cmp(&a.1).then(a.0.cmp(&b.0)));
    Report { trains, top_stages }
}

/// The lines `land-timeline report` prints.
pub fn render(r: &Report) -> Vec<String> {
    let mut out = Vec::new();
    for t in &r.trains {
        out.push(format!(
            "{}: {}s from writer done to merge, bottleneck {} {}s, idle {}s, path {}",
            t.subject,
            t.wall_secs,
            t.bottleneck.0,
            t.bottleneck.1,
            t.idle_secs,
            t.critical_path.join(" > ")
        ));
    }
    if let Some((name, secs)) = r.top_stages.first() {
        out.push(format!(
            "slowest stage over {} trains: {name} {secs}s",
            r.trains.len()
        ));
    }
    out
}

const USAGE: &str = "usage:\n  plangraph land-timeline record --subject S --head H --stage NAME --host HOST --start EPOCH --end EPOCH --result R\n  plangraph land-timeline report [--last N]";

pub fn run(home: &Path, args: &[String]) -> i32 {
    let ledger = home.join(".local/state/plangraph/land-timeline.jsonl");
    let Some((cmd, rest)) = args.split_first() else {
        eprintln!("{USAGE}");
        return 2;
    };
    let mut flags: BTreeMap<&str, &str> = BTreeMap::new();
    let mut it = rest.iter();
    while let Some(k) = it.next() {
        match (k.strip_prefix("--"), it.next()) {
            (Some(k), Some(v)) => {
                flags.insert(k, v);
            }
            _ => {
                eprintln!("{USAGE}");
                return 2;
            }
        }
    }
    let result = match cmd.as_str() {
        "record" => (|| {
            let get = |k: &str| {
                flags
                    .get(k)
                    .map(|v| v.to_string())
                    .ok_or_else(|| format!("missing --{k}"))
            };
            let num = |k: &str| {
                get(k)?
                    .parse::<i64>()
                    .map_err(|_| format!("--{k} must be epoch seconds"))
            };
            append(
                &ledger,
                &Stage {
                    subject: get("subject")?,
                    head: get("head")?,
                    stage: get("stage")?,
                    host: get("host")?,
                    start: num("start")?,
                    end: num("end")?,
                    result: get("result")?,
                },
            )
        })()
        .map(|_| vec![]),
        "report" => load(&ledger).and_then(|s| {
            let n = flags.get("last").map_or(Ok(10), |v| {
                v.parse::<usize>()
                    .map_err(|_| "--last must be a number".to_owned())
            })?;
            Ok(render(&report(&s, n)))
        }),
        _ => Err(USAGE.to_owned()),
    };
    match result {
        Ok(lines) => {
            lines.iter().for_each(|l| println!("{l}"));
            0
        }
        Err(e) => {
            eprintln!("{e}");
            2
        }
    }
}
