//! The stage timeline of a batch or train from writer done to merge, and the report that names the
//! bottleneck stage per train and over the last N trains. The rows are the keeper's `landing_stages` and
//! `landing_ci_jobs` tables, read through the keeper's `land` timeline action (see `landing_report`).
use serde_json::Value;
use std::collections::BTreeMap;

/// One stage run: start, end (epoch seconds), host and result.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Stage {
    pub subject: String,
    pub head: String,
    /// A keeper stage name (`fast_checks`, `push`, `review`, `go`, `merge`, `host_sync`, ...), the push gate's
    /// `fast_checks:<family>`, or `ci:<job>`.
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

/// The stages a landed train must have a row for, in keeper stage names: the push gate's fast checks, the push,
/// CI (the importer's job rows), the writer's review, GO, merge and host sync. Each has a production
/// producer: the pre-push hook and preflight, the lander, the CI importer, `plangraph review`, `go-req`.
pub const REQUIRED: [&str; 7] = [
    "fast_checks",
    "push",
    "ci",
    "review",
    "go",
    "merge",
    "host_sync",
];

/// The push gate's own stages (`fast_checks:<family>`, one per preflight family that ran) must be present too;
/// a train whose gate was skipped or never reported has none.
pub const PUSH_GATE_STAGES: &str = "push_gate_stages";

/// The stages of one batch or train from the keeper's `timeline` reply: its `landing_stages` rows (every
/// attempt is a stage run) and one `ci:<job>` stage per `landing_ci_jobs` row. A `ci` row is dropped for a head
/// that has job rows, so the jobs, not the whole run, are what the path shows.
pub fn from_keeper(subject: &str, data: &Value) -> Vec<Stage> {
    let ms = |v: &Value, k: &str| v[k].as_i64().map(|t| t / 1000);
    let jobs = data["jobs"].as_array().map(Vec::as_slice).unwrap_or(&[]);
    let job_heads: Vec<&str> = jobs.iter().filter_map(|j| j["head"].as_str()).collect();
    let mut out = Vec::new();
    for r in data["stages"].as_array().map(Vec::as_slice).unwrap_or(&[]) {
        let (Some(stage), Some(head), Some(start), Some(end)) = (
            r["stage"].as_str(),
            r["head"].as_str(),
            ms(r, "started_at_ms"),
            ms(r, "ended_at_ms"),
        ) else {
            continue;
        };
        if stage == "ci" && job_heads.contains(&head) {
            continue;
        }
        let text = |k: &str| r[k].as_str().filter(|v| !v.is_empty());
        out.push(Stage {
            subject: subject.into(),
            head: head.into(),
            stage: match text("detail") {
                Some(detail) => format!("{stage}:{detail}"),
                None => stage.into(),
            },
            host: text("host").unwrap_or("unknown").into(),
            start,
            end,
            result: text("result").unwrap_or("unknown").into(),
        });
    }
    for j in jobs {
        let (Some(name), Some(head), Some(start), Some(end)) = (
            j["name"].as_str(),
            j["head"].as_str(),
            ms(j, "started_at_ms"),
            ms(j, "ended_at_ms"),
        ) else {
            continue;
        };
        out.push(Stage {
            subject: subject.into(),
            head: head.into(),
            stage: format!("ci:{name}"),
            host: "github".into(),
            start,
            end,
            result: j["conclusion"].as_str().unwrap_or("unknown").into(),
        });
    }
    out
}

/// The required stages a subject has no row for; a landed train with a gap is reported as incomplete.
pub fn missing(stages: &[Stage]) -> Vec<&'static str> {
    let mut out: Vec<&'static str> = REQUIRED
        .iter()
        .copied()
        .filter(|name| {
            !stages
                .iter()
                .any(|s| s.stage == *name || (*name == "ci" && s.stage.starts_with("ci:")))
        })
        .collect();
    if !stages.iter().any(|s| s.stage.starts_with("fast_checks:")) {
        out.push(PUSH_GATE_STAGES);
    }
    out
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
    /// Writer done to merge, wall clock: it ends where the merge stage ends.
    pub wall_secs: i64,
    /// From the merge to the last post-merge stage (host sync), reported apart from the wall time.
    pub post_merge_secs: i64,
    pub critical_path: Vec<String>,
    /// The longest stage on the critical path, with its seconds.
    pub bottleneck: (String, i64),
    /// Time on the critical path spent between stages (waiting for a person or a queue).
    pub idle_secs: i64,
    /// Required stages with no row.
    pub missing: Vec<&'static str>,
}

#[derive(Debug, PartialEq, Eq)]
pub struct Report {
    pub trains: Vec<TrainReport>,
    /// Critical-path seconds per stage name over the last N trains, largest first.
    pub top_stages: Vec<(String, i64)>,
}

/// The report over the trains of `roster` (newest first, the keeper's landing order), at most `last_n`. A train
/// with no stage row is kept and reported with every required stage missing. Stage names keep their suffix:
/// `ci:rust` and `ci:browser` are different stages.
pub fn report_roster(roster: &[String], stages: &[Stage], last_n: usize) -> Report {
    let mut by: BTreeMap<&str, Vec<Stage>> = BTreeMap::new();
    for s in stages {
        by.entry(s.subject.as_str()).or_default().push(s.clone());
    }
    let mut trains = Vec::new();
    let mut totals: BTreeMap<String, i64> = BTreeMap::new();
    for subject in roster.iter().take(last_n) {
        let all = by.get(subject.as_str()).map_or(&[][..], Vec::as_slice);
        let first = all.iter().map(|s| s.start).min().unwrap_or(0);
        let last = all.iter().map(|s| s.end).max().unwrap_or(0);
        // The merge ends the writer-done-to-merge time; stages that end after it (host sync) are post-merge.
        let merged = all
            .iter()
            .filter(|s| s.stage == "merge")
            .map(|s| s.end)
            .max();
        let before: Vec<Stage> = match merged {
            Some(at) => all.iter().filter(|s| s.end <= at).cloned().collect(),
            None => all.to_vec(),
        };
        let path = critical_path(&before);
        let busy: i64 = path.iter().map(Stage::secs).sum();
        let wall = merged.unwrap_or(last) - first;
        let slowest = path.iter().max_by_key(|s| s.secs());
        for s in &path {
            *totals.entry(s.stage.clone()).or_insert(0) += s.secs();
        }
        trains.push(TrainReport {
            subject: subject.clone(),
            wall_secs: wall,
            post_merge_secs: merged.map_or(0, |at| (last - at).max(0)),
            critical_path: path.iter().map(|s| s.stage.clone()).collect(),
            bottleneck: slowest.map_or((String::new(), 0), |s| (s.stage.clone(), s.secs())),
            idle_secs: (wall - busy).max(0),
            missing: missing(all),
        });
    }
    let mut top_stages: Vec<(String, i64)> = totals.into_iter().collect();
    top_stages.sort_by(|a, b| b.1.cmp(&a.1).then(a.0.cmp(&b.0)));
    Report { trains, top_stages }
}

/// The report over the trains named by `stages`, most recently ended first.
pub fn report(stages: &[Stage], last_n: usize) -> Report {
    let mut ends: BTreeMap<&str, i64> = BTreeMap::new();
    for s in stages {
        let end = ends.entry(s.subject.as_str()).or_insert(0);
        *end = (*end).max(s.end);
    }
    let mut order: Vec<(&str, i64)> = ends.into_iter().collect();
    order.sort_by_key(|(_, end)| std::cmp::Reverse(*end));
    let roster: Vec<String> = order.iter().map(|(k, _)| (*k).to_owned()).collect();
    report_roster(&roster, stages, last_n)
}

/// The lines `landing report --last N` prints.
pub fn render(r: &Report) -> Vec<String> {
    let mut out = Vec::new();
    for t in &r.trains {
        out.push(format!(
            "{}: {}s from writer done to merge ({}s after it), bottleneck {} {}s, idle {}s, path {}",
            t.subject,
            t.wall_secs,
            t.post_merge_secs,
            t.bottleneck.0,
            t.bottleneck.1,
            t.idle_secs,
            t.critical_path.join(" > ")
        ));
        if !t.missing.is_empty() {
            let line = out.last_mut().expect("line just pushed");
            line.push_str(&format!(", incomplete: no {}", t.missing.join(", ")));
        }
    }
    if let Some((name, secs)) = r.top_stages.first() {
        out.push(format!(
            "slowest stage over {} trains: {name} {secs}s",
            r.trains.len()
        ));
    }
    out
}
