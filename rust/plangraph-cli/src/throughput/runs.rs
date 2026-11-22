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

    pub fn accepted(&self, arrival: &Arrival) -> Option<Accepted<'_>> {
        self.writers()
            .filter_map(|run| {
                let review = &run["review"];
                let head = review["head"].as_str()?;
                if review["verdict"] != "ACCEPT"
                    || run["final_head"] != head
                    || (head != arrival.sha
                        && arrival.parents.get(1).is_none_or(|parent| parent != head))
                {
                    return None;
                }
                let at = seconds(&review["accepted_at"]).or_else(|| {
                    let reader = self.records.get(review["run_id"].as_str()?)?;
                    seconds(&reader["ended"])
                })?;
                let seconds = arrival.at.checked_sub(at).filter(|seconds| *seconds >= 0)?;
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
                Some(Accepted {
                    seconds,
                    commits: arrival
                        .commits
                        .saturating_sub(u64::from(head != arrival.sha)),
                    model,
                    tokens,
                })
            })
            .min_by_key(|value| value.seconds)
    }

    pub fn quota(&self, now: i64) -> BTreeMap<&str, f64> {
        let Some(data) = &self.quota else {
            return BTreeMap::new();
        };
        data.as_array()
            .unwrap()
            .iter()
            .filter_map(|row| row["provider"].as_str())
            .map(|name| name.split(':').next().unwrap_or(name))
            .collect::<std::collections::BTreeSet<_>>()
            .into_iter()
            .filter_map(|name| {
                crate::feeder::quota::remaining_for(data, name, now, 10_800)
                    .map(|remaining| (name, remaining))
            })
            .collect()
    }
}
