//! Prompt-order measurement: review verdict completion and cached input tokens, before and
//! after the stable-first prompt order, per leg (matched stratum), from recorded runs only.
use crate::{evidence::regular, review_artifacts::directory, review_health::terminal};
use serde_json::{json, Value};
use std::{collections::BTreeMap, io::Read, io::Write, path::Path};

const HELP: &str =
    "health review-order [--state PATH] --since T --until T [--leg ID[,ID]] [--host NAME[,NAME]]";
/// First words of the review output contract line, present in both recorded layouts.
const CONTRACT_LINE: &str = "Name each finding as file:line.";
const CONTRACT_HEADING: &str = "=== REVIEW OUTPUT";
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

/// Launched attempts of a record (a skipped attempt never ran), in order.
fn launched(record: &Value) -> Vec<&Value> {
    record["attempts"]
        .as_array()
        .map(|a| a.iter().filter(|x| x["status"] != "skipped").collect())
        .unwrap_or_default()
}

/// A leg id stored as `{"id":..}` or as a bare string.
fn leg_id(leg: &Value) -> Option<&str> {
    leg["id"].as_str().or_else(|| leg.as_str())
}

/// The leg record that produced the review: the last launched attempt's leg, else the chosen
/// leg of a record with no attempt list. `None` when attempts exist but none launched.
fn final_leg_value(record: &Value) -> Option<&Value> {
    match record["attempts"].as_array().filter(|a| !a.is_empty()) {
        Some(_) => launched(record).last().map(|a| &a["leg"]),
        None => Some(&record["chosen_leg"]),
    }
}

fn final_leg(record: &Value) -> Option<&str> {
    final_leg_value(record).and_then(leg_id)
}

/// What the stored prompt shows about its order. `Closing`: the `=== REVIEW OUTPUT` heading and
/// the contract line are the last content (stable-first). `Leading`: the contract line has no
/// heading and any `=== ` evidence section (item context, inline diff, review material) follows it.
/// Anything else carries no unambiguous layout.
#[derive(Clone, Copy, PartialEq)]
enum Layout {
    Closing,
    Leading,
    Unknown,
}

fn layout_of(text: &str) -> Layout {
    let lines: Vec<&str> = text.lines().collect();
    let Some(at) = lines.iter().rposition(|l| l.starts_with(CONTRACT_LINE)) else {
        return Layout::Unknown;
    };
    let headed = at > 0 && lines[at - 1] == CONTRACT_HEADING;
    let evidence_after = lines[at + 1..].iter().any(|l| l.starts_with("=== "));
    let nothing_after = lines[at + 1..].iter().all(|l| l.trim().is_empty());
    match (headed, evidence_after, nothing_after) {
        (true, false, true) => Layout::Closing,
        (false, true, _) => Layout::Leading,
        _ => Layout::Unknown,
    }
}

/// The stored `prompt.txt` of a run, `None` when none was stored or it is unreadable.
fn stored_prompt(state: &Path, record: &Value) -> Option<String> {
    let path = directory(state, record).ok()?.join("prompt.txt");
    regular(&path, false).ok()?;
    let mut text = String::new();
    std::fs::File::open(path)
        .ok()?
        .take(MAX_LOG + 1)
        .read_to_string(&mut text)
        .ok()?;
    (text.len() as u64 <= MAX_LOG).then_some(text)
}

/// An archived `live-attempt-N.log` means the retry path moved earlier legs' output away, so
/// `live.log` holds the final leg only; otherwise fallback legs share one log.
fn log_is_final_leg_only(state: &Path, record: &Value) -> bool {
    launched(record).len() <= 1
        || directory(state, record)
            .and_then(|d| std::fs::read_dir(d).map_err(Into::into))
            .is_ok_and(|mut entries| {
                entries.any(|e| {
                    e.is_ok_and(|e| e.file_name().to_string_lossy().starts_with("live-attempt-"))
                })
            })
}

#[derive(Default)]
struct Stratum {
    cohorts: [Tally; 2],
    hosts: BTreeMap<String, u64>,
    harnesses: BTreeMap<String, u64>,
    roles: std::collections::BTreeSet<String>,
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
    fn view(&self, label: &str) -> Value {
        json!({"label":label, "reviews":self.reviews,
            "no_verdict":self.no_verdict, "no_verdict_share":share(self.no_verdict, self.reviews),
            "usage_runs":self.usage_runs, "mixed_leg_excluded":self.mixed, "missing_usage":self.reviews - self.usage_runs - self.mixed,
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

/// Compare the two prompt-order cohorts per leg inside one dispatch window. Only legs with
/// reviews in both cohorts are matched and summed; the others are listed so a mismatch is
/// visible.
pub(crate) fn compare(
    state: &Path,
    records: &[Value],
    (since, until): (i64, i64),
    legs: &[String],
    hosts: &[String],
) -> Value {
    let mut tallies: BTreeMap<&str, Stratum> = BTreeMap::new();
    let (mut unlaunched, mut no_prompt, mut no_start, mut unknown, mut unchanged) =
        (0u64, 0u64, 0u64, 0u64, 0u64);
    let layouts: Vec<Option<Layout>> = records
        .iter()
        .map(|r| {
            terminal(state, r)?;
            stored_prompt(state, r).map(|t| layout_of(&t))
        })
        .collect();
    // Rollout exposure per host: the first dispatch that produced a reordered prompt. An old
    // layout dispatched after it was not reordered by the rollout and is no before sample.
    let mut rollout: BTreeMap<&str, f64> = BTreeMap::new();
    for (record, layout) in records.iter().zip(&layouts) {
        if let (Some(Layout::Closing), Some(started)) = (layout, record["started"].as_f64()) {
            let at = rollout
                .entry(record["host"].as_str().unwrap_or("unknown"))
                .or_insert(started);
            *at = at.min(started);
        }
    }
    for (record, layout) in records.iter().zip(layouts) {
        let Some((_, _, class)) = terminal(state, record) else {
            continue; // not a terminal review run
        };
        let Some(started) = record["started"].as_f64() else {
            no_start += 1;
            continue;
        };
        if started < since as f64 || started >= until as f64 {
            continue;
        }
        let leg = final_leg(record);
        if leg.is_some_and(|l| !legs.is_empty() && !legs.iter().any(|x| x == l)) {
            continue;
        }
        let host = record["host"].as_str().unwrap_or("unknown");
        if !hosts.is_empty() && !hosts.iter().any(|h| h == host) {
            continue;
        }
        let Some(leg) = leg else {
            unlaunched += 1;
            continue;
        };
        let cohort = match layout {
            None => {
                no_prompt += 1;
                continue;
            }
            Some(Layout::Leading) if rollout.get(host).is_some_and(|&at| started >= at) => {
                unchanged += 1;
                continue;
            }
            Some(Layout::Leading) => 0,
            Some(Layout::Closing) => 1,
            Some(Layout::Unknown) => {
                unknown += 1;
                continue;
            }
        };
        let stratum = tallies.entry(leg).or_default();
        *stratum.hosts.entry(host.into()).or_default() += 1;
        let attempt_leg = final_leg_value(record).unwrap_or(&Value::Null);
        let harness = attempt_leg["harness"]
            .as_str()
            .or_else(|| record["harness"].as_str())
            .unwrap_or("unknown");
        *stratum.harnesses.entry(harness.into()).or_default() += 1;
        let roles = attempt_leg["roles"].as_array().into_iter().flatten();
        stratum
            .roles
            .extend(roles.filter_map(|r| r.as_str().map(String::from)));
        let tally = &mut stratum.cohorts[cohort];
        tally.reviews += 1;
        tally.no_verdict += u64::from(class != "verdict");
        if !log_is_final_leg_only(state, record) {
            tally.mixed += 1; // fallback legs share one live.log: usage is not per leg
        } else if let Some((input, cached)) = live_log(state, record).as_deref().and_then(usage) {
            tally.usage_runs += 1;
            tally.input += input;
            tally.cached += cached;
        }
    }
    let (mut strata, mut unmatched) = (BTreeMap::new(), Vec::new());
    let (mut sum_before, mut sum_after) = (Tally::default(), Tally::default());
    for (leg, stratum) in &tallies {
        let [b, a] = &stratum.cohorts;
        let matched = b.reviews > 0 && a.reviews > 0;
        if matched {
            sum_before.add(b);
            sum_after.add(a);
        } else {
            unmatched.push(*leg);
        }
        strata.insert(
            *leg,
            json!({"matched":matched, "before":b.view("before"), "after":a.view("after"),
                "provenance":{"hosts":stratum.hosts, "harnesses":stratum.harnesses,
                    "leg_roles":stratum.roles}}),
        );
    }
    json!({"query":"pg-spawnd health review-order: terminal review runs (role review, status \
        completed|succeeded|terminated_after_answer|failed|timeout|timed_out|stalled|limited|killed) \
        with started in [since, until), grouped by the last launched attempt leg (string or object \
        leg); cohort = prompt order read from the stored prompt.txt: after when the \
        === REVIEW OUTPUT heading and the contract line are the last content, before when the \
        contract line has no heading and an === section follows it and the host had not yet produced a \
        reordered prompt (a later one is post_rollout_unreordered), otherwise unknown_layout and \
        excluded; --host keeps the named hosts; no_verdict = review result other than verdict; cached share = cached / total input \
        tokens summed over live.log usage frames. Runs with more than one launched attempt and no \
        archived live-attempt log share one live.log: counted in mixed_leg_excluded, left out of \
        usage and not counted as missing_usage; missing_usage = reviews without any usage frame \
        among the rest",
        "window":{"since":since, "until":until},
        "strata":strata, "unmatched":unmatched,
        "excluded":{"unlaunched":unlaunched,
            "no_prompt":no_prompt, "unknown_layout":unknown,
            "post_rollout_unreordered":unchanged, "no_start_time":no_start},
        "matched_total":{"before":sum_before.view("before"), "after":sum_after.view("after")}})
}

pub(crate) fn execute(args: Vec<String>, out: &mut dyn Write) -> Result<(), String> {
    if args.iter().any(|a| a == "--help" || a == "-h") {
        return writeln!(out, "{HELP}").map_err(|e| e.to_string());
    }
    let mut flags = BTreeMap::new();
    let mut args = args.into_iter();
    while let Some(flag) = args.next() {
        let known = ["--state", "--since", "--until", "--leg", "--host"];
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
    let window = (time("--since")?, time("--until")?);
    if window.0 >= window.1 {
        return Err("window must be non-empty".into());
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
    let hosts: Vec<String> = flags
        .get("--host")
        .map(|s| {
            s.split(',')
                .filter(|h| !h.is_empty())
                .map(String::from)
                .collect()
        })
        .unwrap_or_default();
    let records = crate::records::list_runs(&state);
    let report = compare(&state, &records, window, &legs, &hosts);
    writeln!(out, "{report}").map_err(|e| e.to_string())
}

#[cfg(test)]
#[path = "review_order_tests.rs"]
mod tests;
