use super::{read_input, timestamp, Arrival, Env, HOUR};
use serde_json::Value;
use std::{collections::BTreeMap, fs, path::Path};

#[derive(Default)]
pub(super) struct Inputs {
    records: BTreeMap<String, Value>,
    quota: Option<Value>,
}

#[derive(Default)]
pub(super) struct WriterTime {
    pub running: i64,
    pub measured: i64,
    pub active: i64,
}

pub(super) struct Accepted<'a> {
    pub seconds: i64,
    pub commits: u64,
    pub model: Option<&'a str>,
    pub tokens: Option<u64>,
}

fn seconds(value: &Value) -> Option<i64> {
    value
        .as_f64()
        .filter(|n| n.is_finite())
        .map(|n| n as i64)
        .or_else(|| value.as_str().and_then(|text| timestamp(text).ok()))
}

fn overlap(start: i64, end: i64, hour: i64, cutoff: i64) -> i64 {
    (end.min(cutoff).min(hour + HOUR) - start.max(hour)).max(0)
}

impl Inputs {
    pub fn load(env: &Env) -> Result<Self, String> {
        let mut inputs = Self::default();
        for path in env
            .get("PLANGRAPH_THROUGHPUT_RUNS")
            .into_iter()
            .flat_map(std::env::split_paths)
        {
            for entry in
                fs::read_dir(path).map_err(|e| format!("cannot read throughput runs: {e}"))?
            {
                let entry = entry.map_err(|e| e.to_string())?;
                if !entry.file_type().map_err(|e| e.to_string())?.is_dir() {
                    continue;
                }
                let path = entry.path().join("record.json");
                if !path.is_file() {
                    continue;
                }
                let mut record = read_input(&path)?;
                let Some(id) = record["run_id"].as_str().map(str::to_owned) else {
                    continue;
                };
                if record["run_kind"] == "native-codex" {
                    if record["origin_receipt"] == true || record["overflow_remote"] == true {
                        continue;
                    }
                    match fs::read_to_string(entry.path().join("ledger.jsonl")) {
                        Ok(log) => {
                            let attempts: Vec<Value> = log
                                .lines()
                                .map(serde_json::from_str)
                                .collect::<Result<_, _>>()
                                .map_err(|e| format!("invalid writer ledger: {e}"))?;
                            record["throughput_attempts"] = Value::Array(attempts);
                        }
                        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
                        Err(e) => return Err(e.to_string()),
                    }
                }
                inputs.records.insert(id, record);
            }
        }
        if let Some(path) = env.get("PLANGRAPH_THROUGHPUT_QUOTA") {
            let quota = read_input(Path::new(path))?;
            if !quota.is_array() {
                return Err("quota snapshot must be an array".into());
            }
            inputs.quota = Some(quota);
        }
        Ok(inputs)
    }

    fn writers(&self) -> impl Iterator<Item = &Value> {
        self.records.values().filter(|r| {
            r["run_kind"] == "native-codex"
                && r["origin_receipt"] != true
                && r["overflow_remote"] != true
        })
    }

    pub fn writer_time(&self, hour: i64, cutoff: i64) -> BTreeMap<&str, WriterTime> {
        let mut hosts: BTreeMap<&str, WriterTime> = BTreeMap::new();
        for run in self.writers() {
            let Some(host) = run["host"].as_str() else {
                continue;
            };
            let time = hosts.entry(host).or_default();
            let mut attempts = BTreeMap::new();
            for attempt in run["throughput_attempts"]
                .as_array()
                .into_iter()
                .flatten()
                .chain(std::iter::once(run))
            {
                if let Some(start) = seconds(&attempt["started"]) {
                    let end = seconds(&attempt["ended"])
                        .or_else(|| (attempt["status"] == "running").then_some(cutoff));
                    if let Some(end) = end {
                        attempts.insert(start, end);
                    }
                }
            }
            for (start, end) in attempts {
                let duration = overlap(start, end, hour, cutoff);
                time.running += duration;
                if let Some(turns) = run["writer_turns"].as_array() {
                    let mut observed = false;
                    let active = turns
                        .iter()
                        .filter_map(|turn| {
                            let begin = seconds(&turn["started"])?;
                            if !(start..end).contains(&begin) {
                                return None;
                            }
                            observed = true;
                            let finish = seconds(&turn["ended"]).unwrap_or(cutoff).min(end);
                            Some(overlap(begin, finish, hour, cutoff))
                        })
                        .sum::<i64>();
                    if observed {
                        time.measured += duration;
                        time.active += active.min(duration);
                    }
                }
            }
        }
        hosts
    }

    /// One acceptance per verified included part: every ACCEPT of a writer's final head that is a commit the
    /// arrival made reachable, joined to that head, with the commits only that part brought.
    pub fn accepted_parts(&self, arrival: &Arrival) -> Vec<(String, Accepted<'_>)> {
        let mut found: BTreeMap<&str, Accepted<'_>> = BTreeMap::new();
        for run in self.writers() {
            let review = &run["review"];
            let Some(head) = review["head"].as_str() else {
                continue;
            };
            if review["verdict"] != "ACCEPT"
                || run["final_head"] != head
                || !arrival.members.iter().any(|(sha, _)| sha == head)
                || arrival
                    .part_heads
                    .as_ref()
                    .is_some_and(|parts| !parts.iter().any(|part| part == head))
            {
                continue;
            }
            let Some(at) = seconds(&review["accepted_at"]).or_else(|| {
                let reader = self.records.get(review["run_id"].as_str()?)?;
                seconds(&reader["ended"])
            }) else {
                continue;
            };
            let Some(wait) = arrival.at.checked_sub(at).filter(|wait| *wait >= 0) else {
                continue;
            };
            let usage = run["throughput_attempts"].as_array();
            let model = usage.and_then(|rows| rows.first()?.get("model")?.as_str());
            let tokens = usage.zip(model).and_then(|(rows, model)| {
                rows.iter().try_fold(0_u64, |total, row| {
                    if row["model"] != model {
                        return None;
                    }
                    total.checked_add(row["tokens"].as_u64()?)
                })
            });
            let candidate = Accepted {
                seconds: wait,
                commits: 0,
                model,
                tokens,
            };
            match found.get(head) {
                Some(best) if best.seconds <= wait => {}
                _ => {
                    found.insert(head, candidate);
                }
            }
        }
        // Oldest part first: a commit counts for the first part that contains it.
        let parents_of: std::collections::HashMap<&str, &Vec<String>> = arrival
            .members
            .iter()
            .map(|(sha, parents)| (sha.as_str(), parents))
            .collect();
        let mut covered = std::collections::HashSet::new();
        let mut parts = Vec::new();
        for (sha, _) in &arrival.members {
            let Some(mut accepted) = found.remove(sha.as_str()) else {
                continue;
            };
            let mut stack = vec![sha.as_str()];
            let mut own = 0;
            while let Some(next) = stack.pop() {
                let Some(parents) = parents_of.get(next) else {
                    continue;
                };
                if covered.insert(next.to_owned()) {
                    own += 1;
                    stack.extend(parents.iter().map(String::as_str));
                }
            }
            accepted.commits = own;
            parts.push((sha.clone(), accepted));
        }
        parts
    }

    /// Latest quota window of every provider and account at `now`, with its reset; stale readings keep a NULL percentage.
    pub fn quota(&self, now: i64) -> Vec<crate::feeder::quota::Window> {
        self.quota
            .as_ref()
            .map(|data| crate::feeder::quota::windows(data, now, 10_800))
            .unwrap_or_default()
    }
}
