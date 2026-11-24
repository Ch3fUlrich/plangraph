//! Prompt-order measurement: review verdict completion and cached input tokens, before and
//! after the stable-first prompt order, per leg (matched stratum), from recorded runs only.
use crate::{evidence::regular, review_artifacts::directory, review_health::sample};
use serde_json::{json, Value};
use std::{collections::BTreeMap, io::Read, io::Write, path::Path};

const HELP: &str = "health review-order [--state PATH] --before-since T --before-until T --after-since T --after-until T [--leg ID[,ID]]";
const MAX_LOG: u64 = 8 * 1024 * 1024;

/// `(total input tokens, cached input tokens)` summed over the usage frames of a live log.
/// Opencode `step_finish` frames count `tokens.input` without the cache, so the cache read and
/// write are added to the total (only the read counts as cached, as in `governor.metrics`); Codex `turn.completed` frames already include `cached_input_tokens`.
pub(crate) fn usage(log: &str) -> Option<(u64, u64)> {
    let mut seen = None;
    for frame in log
        .lines()
        .filter_map(|l| serde_json::from_str::<Value>(l).ok())
    {
        let (input, cached) = match frame["type"].as_str() {
            Some("step_finish") => {
                let t = &frame["part"]["tokens"];
                let cached = t["cache"]["read"].as_u64().unwrap_or(0);
                let written = t["cache"]["write"].as_u64().unwrap_or(0);
                let total = t["input"]
                    .as_u64()
                    .map(|i| i.saturating_add(cached).saturating_add(written));
                (total, cached)
            }
            Some("turn.completed") => (
                frame["usage"]["input_tokens"].as_u64(),
                frame["usage"]["cached_input_tokens"].as_u64().unwrap_or(0),
            ),
            _ => continue,
        };
        let Some(input) = input else { continue };
        let (i, c) = seen.unwrap_or((0u64, 0u64));
        seen = Some((i.saturating_add(input), c.saturating_add(cached)));
    }
    seen
}

fn live_log(state: &Path, record: &Value) -> Option<String> {
    let path = directory(state, record).ok()?.join("live.log");
    regular(&path, false).ok()?;
    let mut text = String::new();
    std::fs::File::open(path)
        .ok()?
        .take(MAX_LOG + 1)
        .read_to_string(&mut text)
        .ok()?;
    (text.len() as u64 <= MAX_LOG).then_some(text)
}

/// Attempts that launched a leg (a skipped attempt never wrote to the log).
fn launched_attempts(record: &Value) -> usize {
    record["attempts"]
        .as_array()
        .map_or(0, |a| a.iter().filter(|x| x["status"] != "skipped").count())
}

#[derive(Default)]
struct Tally {
    reviews: u64,
    no_verdict: u64,
    usage_runs: u64,
    mixed: u64,
    input: u64,
    cached: u64,
}

fn share(part: u64, whole: u64) -> Value {
    if whole == 0 {
        Value::Null
    } else {
        json!(part as f64 / whole as f64)
    }
}

impl Tally {
    fn view(&self, label: &str, (since, until): (i64, i64)) -> Value {
        json!({"label":label, "since":since, "until":until, "reviews":self.reviews,
            "no_verdict":self.no_verdict, "no_verdict_share":share(self.no_verdict, self.reviews),
            "usage_runs":self.usage_runs, "mixed_leg_excluded":self.mixed, "missing_usage":self.reviews - self.usage_runs,
            "input_tokens":self.input, "cached_input_tokens":self.cached,
            "cached_input_share":share(self.cached, self.input)})
    }
    fn add(&mut self, other: &Tally) {
        self.reviews += other.reviews;
        self.no_verdict += other.no_verdict;
        self.usage_runs += other.usage_runs;
        self.mixed += other.mixed;
        self.input += other.input;
        self.cached += other.cached;
    }
}

/// Compare two windows per leg. Only legs with reviews in both windows are matched and
/// summed; the others are listed so a mismatch is visible.
pub(crate) fn compare(
    state: &Path,
    records: &[Value],
    before: (i64, i64),
    after: (i64, i64),
    legs: &[String],
) -> Value {
    let mut tallies: BTreeMap<&str, [Tally; 2]> = BTreeMap::new();
    for record in records {
        let Some((leg, (ended, _, class))) = sample(state, record) else {
            continue;
        };
        if !legs.is_empty() && !legs.iter().any(|l| l == leg) {
            continue;
        }
        let window = [before, after]
            .iter()
            .position(|&(since, until)| ended >= since as f64 && ended < until as f64);
        let Some(window) = window else { continue };
        let tally = &mut tallies.entry(leg).or_default()[window];
        tally.reviews += 1;
        tally.no_verdict += u64::from(class != "verdict");
        if launched_attempts(record) > 1 {
            tally.mixed += 1; // fallthrough legs share one live.log: usage is not per leg
        } else if let Some((input, cached)) = live_log(state, record).as_deref().and_then(usage) {
            tally.usage_runs += 1;
            tally.input += input;
            tally.cached += cached;
        }
    }
    let (mut strata, mut unmatched) = (BTreeMap::new(), Vec::new());
    let (mut sum_before, mut sum_after) = (Tally::default(), Tally::default());
    for (leg, [b, a]) in &tallies {
        let matched = b.reviews > 0 && a.reviews > 0;
        if matched {
            sum_before.add(b);
            sum_after.add(a);
        } else {
            unmatched.push(*leg);
        }
        strata.insert(
            *leg,
            json!({"matched":matched, "before":b.view("before", before), "after":a.view("after", after)}),
        );
    }
    json!({"query":"pg-spawnd health review-order: terminal review runs (role review, status \
        completed|succeeded|terminated_after_answer|failed|timeout|timed_out|stalled|limited|killed) \
        by ended in [since, until), grouped by last attempt leg; no_verdict = review result other \
        than verdict; cached share = cached / total input tokens summed over live.log usage \
        frames; runs with more than one launched attempt share one live.log and are excluded from usage \
        (mixed_leg_excluded, also in missing_usage); runs without usage frames count as missing_usage and stay out of the token share",
        "strata":strata, "unmatched":unmatched,
        "matched_total":{"before":sum_before.view("before", before), "after":sum_after.view("after", after)}})
}

pub(crate) fn execute(args: Vec<String>, out: &mut dyn Write) -> Result<(), String> {
    if args.iter().any(|a| a == "--help" || a == "-h") {
        return writeln!(out, "{HELP}").map_err(|e| e.to_string());
    }
    let mut flags = BTreeMap::new();
    let mut args = args.into_iter();
    while let Some(flag) = args.next() {
        let known = [
            "--state",
            "--before-since",
            "--before-until",
            "--after-since",
            "--after-until",
            "--leg",
        ];
        if !known.contains(&flag.as_str()) || flags.contains_key(&flag) {
            return Err("unknown or duplicate review-order flag".into());
        }
        let value = args
            .next()
            .filter(|a| !a.starts_with("--"))
            .ok_or("missing review-order flag value")?;
        flags.insert(flag, value);
    }
    let time = |name: &str| {
        crate::health_cli::timestamp(flags.get(name).ok_or(format!("missing {name}"))?)
    };
    let before = (time("--before-since")?, time("--before-until")?);
    let after = (time("--after-since")?, time("--after-until")?);
    if before.0 >= before.1 || after.0 >= after.1 || before.1 > after.0 {
        return Err(
            "windows must be non-empty and before must end by the time after starts".into(),
        );
    }
    let state = crate::state::resolve_state(flags.get("--state").map(String::as_str), |k| {
        std::env::var_os(k)
    })?;
    let legs: Vec<String> = flags
        .get("--leg")
        .map(|s| {
            s.split(',')
                .filter(|l| !l.is_empty())
                .map(String::from)
                .collect()
        })
        .unwrap_or_default();
    let records = crate::records::list_runs(&state);
    let report = compare(&state, &records, before, after, &legs);
    writeln!(out, "{report}").map_err(|e| e.to_string())
}

#[cfg(test)]
#[path = "review_order_tests.rs"]
mod tests;
