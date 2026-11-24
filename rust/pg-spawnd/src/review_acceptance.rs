//! Replay counted seat passes and head acceptance from exact-run admission evidence.
use pg_worker_core::run::review_signal;
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, BTreeSet},
    path::Path,
};

const THREE_HOURS: i64 = 3 * 3600;
const DAY: i64 = 24 * 3600;
const SILENCE: f64 = 2.0 * 3600.0;

#[derive(Default)]
struct Seat {
    verdicts: u64,
    accepts: u64,
    rounds: Vec<u64>,
    first_low_at: Option<(f64, u64, u64)>,
}

impl Seat {
    fn add(&mut self, pass: bool, round: Option<u64>) {
        self.verdicts += 1;
        self.accepts += u64::from(pass);
        if let Some(round) = round.filter(|_| pass) {
            self.rounds.push(round);
        }
    }
    fn below_threshold(&self) -> bool {
        self.verdicts >= 10 && self.accepts * 100 < self.verdicts * 15
    }

    fn value(mut self) -> Value {
        self.rounds.sort_unstable();
        let median = if self.rounds.is_empty() {
            None
        } else {
            let n = self.rounds.len();
            Some((self.rounds[(n - 1) / 2] as f64 + self.rounds[n / 2] as f64) / 2.0)
        };
        json!({"verdicts":self.verdicts, "accepts":self.accepts,
               "accept_rate":self.accepts as f64 / self.verdicts as f64,
               "median_rounds_to_accept":median,"known_accept_rounds":self.rounds.len()})
    }
}

fn number(value: &Value) -> Option<f64> {
    value.as_f64().filter(|n| n.is_finite())
}

fn timestamp(value: &Value) -> Option<f64> {
    number(value).or_else(|| {
        let parsed = chrono::DateTime::parse_from_rfc3339(value.as_str()?).ok()?;
        Some(parsed.timestamp_millis() as f64 / 1000.0)
    })
}

fn head(value: &Value) -> Option<&str> {
    value
        .as_str()
        .filter(|s| s.len() == 40 && s.bytes().all(|b| b.is_ascii_hexdigit()))
}

fn local(state: &Path, record: &Value) -> Value {
    let mut result = review_signal::fields(record);
    let terminal = &mut result["review_terminal"];
    if terminal.is_object() && terminal["verdict"].is_null() {
        if let Some(verdict) = review_signal::answer_verdict(
            record,
            Some(&crate::review_health::answer(state, record)),
        ) {
            terminal["verdict"] = json!(verdict);
        }
    }
    result["run_id"] = record["run_id"].clone();
    result["started"] = record["started"].clone();
    result["ended"] = record["ended"].clone();
    result
}

type Seats<'a> = BTreeMap<i64, BTreeMap<&'a str, Seat>>;

fn windows(rows: Seats<'_>, width: i64) -> Vec<Value> {
    rows.into_iter()
        .map(|(start, seats)| {
            let seats: BTreeMap<_, _> = seats
                .into_iter()
                .map(|(leg, count)| (leg, count.value()))
                .collect();
            json!({"since":start, "until":start+width, "seats":seats})
        })
        .collect()
}

fn bucket(ended: f64, width: i64) -> i64 {
    (ended.floor() as i64).div_euclid(width) * width
}

// One alert per acceptance gap. Locate the first instant where two hours have
// elapsed and a real review overlaps those two hours.
fn silence_alerts(
    since: f64,
    until: f64,
    accepted: impl Iterator<Item = f64>,
    mut activity: Vec<(f64, f64)>,
) -> Vec<Value> {
    let mut accepted: Vec<_> = accepted.filter(|at| *at >= since && *at < until).collect();
    accepted.sort_unstable_by(f64::total_cmp);
    activity.sort_unstable_by(|a, b| a.0.total_cmp(&b.0));
    let mut previous = since;
    let mut first = 0;
    let mut alerts = Vec::new();
    for end in accepted.into_iter().chain([until]) {
        let minimum = previous + SILENCE;
        if minimum < end {
            while first < activity.len() && activity[first].1 <= previous {
                first += 1;
            }
            if let Some(&(started, stopped)) = activity[first..].first() {
                let at = minimum.max(started);
                if at < end && stopped > at - SILENCE {
                    alerts.push(json!({"kind":"no_head_accepted","at":at}));
                }
            }
        }
        previous = end;
    }
    alerts
}

pub(crate) fn report(
    state: &Path,
    records: &[Value],
    fleet: &Value,
    since: i64,
    until: i64,
) -> Value {
    let local: Vec<_> = records.iter().map(|run| local(state, run)).collect();
    let mut runs = BTreeMap::new();
    for row in fleet["runs"].as_array().into_iter().flatten().chain(&local) {
        if let Some(id) = row["run_id"]
            .as_str()
            .filter(|_| row["origin_receipt"] != true)
        {
            let current = runs.entry(id).or_insert(row);
            if (
                number(&row["ended"]).is_some(),
                row["review_terminal"]["verdict"].is_string(),
            ) >= (
                number(&current["ended"]).is_some(),
                current["review_terminal"]["verdict"].is_string(),
            ) {
                *current = row;
            }
        }
    }
    let mut native = BTreeMap::new();
    for row in runs.values() {
        for review in row["native_review_rounds"].as_array().into_iter().flatten() {
            if let (Some(id), Some(head)) = (review["run_id"].as_str(), head(&review["head"])) {
                native.insert((id, head), review);
            }
        }
    }
    let keeper: BTreeMap<_, _> = fleet["keeper_seats"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|r| Some(((r["seat_run"].as_str()?, head(&r["head"])?), r)))
        .collect();
    let mut accepted = BTreeMap::<&str, f64>::new();
    let mut contributing_runs = BTreeSet::new();
    for row in fleet["head_acceptances"].as_array().into_iter().flatten() {
        if let (Some(sha), Some(at)) = (head(&row["head"]), timestamp(&row["accepted_at"])) {
            accepted
                .entry(sha)
                .and_modify(|earliest| *earliest = earliest.min(at))
                .or_insert(at);
            if at < until as f64 {
                for run in row["seat_runs"]
                    .as_array()
                    .into_iter()
                    .flatten()
                    .filter_map(Value::as_str)
                {
                    contributing_runs.insert((run, sha));
                }
            }
        }
    }
    let mut activity = Vec::new();
    let mut outcomes = Vec::new();
    let mut samples = BTreeMap::<(String, String), Value>::new();
    let (from, to) = (since as f64, until as f64);
    for (id, row) in runs {
        let ended = number(&row["ended"]);
        let active_until = ended.unwrap_or(to).min(to);
        if let Some(started) = number(&row["started"])
            .filter(|&t| row["review_activity"] == true && t < active_until && active_until > from)
        {
            activity.push((started, active_until));
        }
        let terminal = &row["review_terminal"];
        let (Some(ended), Some(leg)) = (ended, terminal["leg"].as_str()) else {
            continue;
        };
        let sha = head(&terminal["head"]);
        let key = sha.map(|sha| (id, sha));
        let evidence = key.and_then(|k| native.get(&k)).filter(|r| r["leg"] == leg);
        let keep = key.and_then(|k| keeper.get(&k));
        let native_accept = evidence.filter(|r| r["verdict"] == "ACCEPT");
        if let (Some(sha), Some(review)) = (sha, native_accept) {
            let at = timestamp(&review["accepted_at"]).unwrap_or(ended);
            accepted
                .entry(sha)
                .and_modify(|earliest| *earliest = earliest.min(at))
                .or_insert(at);
            if at < to {
                contributing_runs.insert((id, sha));
            }
        }
        if (from..to).contains(&ended) {
            let (pass, round) = if terminal["keeper_bound"] == true || keep.is_some() {
                match keep {
                    Some(receipt) if receipt["pass"].is_boolean() => {
                        (receipt["pass"] == true, receipt["round"].as_u64())
                    }
                    _ => continue,
                }
            } else if let Some(review) = evidence {
                (review["verdict"] == "ACCEPT", review["round"].as_u64())
            } else if key.is_some_and(|key| contributing_runs.contains(&key)) {
                (true, None)
            } else if terminal["verdict"] == "changes" {
                (false, None)
            } else {
                continue;
            };
            outcomes.push((ended, leg, id, pass, round));
            if let (Some(sha), Some(revision)) = (
                sha,
                row["counted_review"]["selection"]["seat"]["revision"].as_str(),
            ) {
                let key = (sha.to_owned(), revision.to_owned());
                let sample =
                    json!({"binding":row["counted_review"], "seat_pass":pass, "round":round});
                let stored = samples.entry(key).or_insert_with(|| sample.clone());
                if pass {
                    *stored = sample;
                }
            }
        }
    }
    outcomes.sort_unstable_by(|a, b| a.0.total_cmp(&b.0).then(a.2.cmp(b.2)));
    let mut three_hours = Seats::new();
    let mut daily = Seats::new();
    let mut alerts = Vec::new();
    for (ended, leg, _, pass, round) in outcomes {
        let start = bucket(ended, THREE_HOURS);
        let seat = three_hours
            .entry(start)
            .or_default()
            .entry(leg)
            .or_default();
        seat.add(pass, round);
        daily
            .entry(bucket(ended, DAY))
            .or_default()
            .entry(leg)
            .or_default()
            .add(pass, round);
        if seat.below_threshold() && seat.first_low_at.is_none() {
            seat.first_low_at = Some((ended, seat.verdicts, seat.accepts));
        }
    }
    for (start, seats) in &three_hours {
        for (leg, seat) in seats {
            if let Some((at, verdicts, accepts)) =
                seat.first_low_at.filter(|_| seat.below_threshold())
            {
                alerts.push(json!({"kind":"low_acceptance","leg":leg,"at":at,
                    "window_since":start,"verdicts":verdicts,"accepts":accepts}));
            }
        }
    }
    let mut comparison_samples = Vec::new();
    for ((sha, _), mut sample) in samples {
        let accepted_in_window = sample["binding"]["run_id"]
            .as_str()
            .is_some_and(|id| contributing_runs.contains(&(id, sha.as_str())));
        if sample["seat_pass"] == true && !accepted_in_window {
            continue;
        }
        sample["accepted"] = json!(sample["seat_pass"] == true && accepted_in_window);
        sample.as_object_mut().unwrap().remove("seat_pass");
        comparison_samples.push(sample);
    }
    let complete = fleet.is_null() || fleet["coverage"]["complete"] == true;
    if complete {
        alerts.extend(silence_alerts(
            from,
            to,
            accepted.values().copied(),
            activity,
        ));
    }
    alerts.sort_unstable_by(|a, b| a["at"].as_f64().partial_cmp(&b["at"].as_f64()).unwrap());
    json!({"windows_3h":windows(three_hours, THREE_HOURS), "days":windows(daily, DAY),
        "alerts":alerts, "comparison_samples":comparison_samples, "coverage": if fleet.is_null() { json!({"scope":"local"}) }
                                     else { fleet["coverage"].clone() }})
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fleet_deduplicates_serving_runs_and_requires_coverage_for_silence() {
        let state = tempfile::tempdir().unwrap();
        let head = "a".repeat(40);
        let run = json!({"run_id":"run-1","started":10,"ended":20,
            "review_activity":true,"review_terminal":{"keeper_bound":true,"leg":"seat","head":head}});
        let mut fleet = json!({"coverage":{"scope":"fleet","complete":false},
            "runs":[run,run,{"run_id":"run-active","started":8000,"review_activity":true}],
            "keeper_seats":[{"seat_run":"run-1","head":head,"pass":true,"round":3}],
            "head_acceptances":[]});
        let view = report(state.path(), &[], &fleet, 0, 9000);
        assert_eq!(view["windows_3h"][0]["seats"]["seat"]["accepts"], 1);
        assert_eq!(
            view["windows_3h"][0]["seats"]["seat"]["median_rounds_to_accept"],
            3.0
        );
        assert_eq!(view["alerts"], json!([]));
        fleet["coverage"]["complete"] = json!(true);
        let view = report(state.path(), &[], &fleet, 0, 9000);
        assert_eq!(
            view["alerts"],
            json!([{"kind":"no_head_accepted","at":7200.0}])
        );
        fleet["head_acceptances"] = json!([{"head":head,"accepted_at":5000,
                                            "seat_runs":["run-1"]}]);
        let view = report(state.path(), &[], &fleet, 0, 9000);
        assert_eq!(view["alerts"], json!([]));
    }
}
