//! How many items one batch brief carries: computed from the writer model and the items'
//! expected size, never chosen by hand (operator 2026-11-20).
//!
//! The count is the smallest of three bounds. (a) The model cap: 10 for a strong writer
//! (catalog intelligence 50 or more) with a context window of 400k tokens or more, 5 for a
//! strong writer with a smaller window, 3 otherwise. (b) The fit: the items' expected diff
//! bytes, taken in order, stay under a planning target below the hard limit so the fix rounds
//! still fit; an item expected above half the limit goes alone. (c) The feedback of the last
//! batch: one item fewer after skipped items or a HIGH that survived two rounds, one more
//! after a batch accepted in round 1.
use crate::paths::{paths_overlap, repo_relative};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};
use std::path::{Component, Path, PathBuf};

#[cfg(test)]
#[path = "batch_size_tests.rs"]
mod tests;

/// Catalog intelligence of a strong writer (rule 3.41).
pub const STRONG_INTELLIGENCE: f64 = 50.0;
/// Context window, in tokens, from which a strong writer carries the largest batch.
pub const LARGE_WINDOW: u64 = 400_000;
/// Interim hard limit of a checkpoint diff; the ledger replaces it with a measured value.
pub const DEFAULT_LIMIT_BYTES: u64 = 100_000;
/// The planning target is this share of the limit, so the fix rounds still fit under it.
pub const TARGET_PERCENT: u64 = 60;
/// Expected diff bytes of an item with no estimate and no merged item of any kind to take a median of.
pub const UNKNOWN_ITEM_BYTES: u64 = 30_000;

#[derive(Debug, Deserialize)]
pub struct Writer {
    pub aa_intelligence: Option<f64>,
    pub context_window: Option<u64>,
}

#[derive(Debug, Deserialize)]
pub struct Item {
    pub key: String,
    pub kind: String,
    pub expected_bytes: Option<u64>,
    /// The item text (Why, How, Done when) the brief carries; `brief` refuses an item without.
    #[serde(default)]
    pub text: Option<String>,
    /// Repository-relative files or module paths from the planner's file list.
    #[serde(default)]
    pub files: Vec<String>,
    pub design_ref: Option<String>,
}

#[derive(Clone, Copy, Debug, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum Outcome {
    /// Items were skipped.
    Skipped,
    /// A HIGH finding survived two rounds.
    HighSurvived,
    /// The batch was accepted in round 1.
    AcceptedRound1,
    Neutral,
}

#[derive(Debug, Deserialize)]
pub struct Last {
    pub count: usize,
    pub outcome: Outcome,
}

#[derive(Debug, Deserialize)]
pub struct Input {
    pub writer: Writer,
    pub items: Vec<Item>,
    /// Checkout roots keyed by the qualifier used in a design reference.
    #[serde(default)]
    pub design_roots: BTreeMap<String, PathBuf>,
    /// File lists of the parts already assigned to this train, combined by the caller.
    #[serde(default)]
    pub prior_part_files: Vec<String>,
    /// Diff bytes of merged items by kind: the fallback estimate is their median.
    #[serde(default)]
    pub merged_bytes: BTreeMap<String, Vec<u64>>,
    pub limit_bytes: Option<u64>,
    pub last: Option<Last>,
    /// The `NAME: value` lines of the brief's Run values block, in key order.
    #[serde(default)]
    pub run_values: BTreeMap<String, String>,
}

#[derive(Debug, PartialEq, Serialize)]
pub struct Plan {
    pub count: usize,
    pub keys: Vec<String>,
    /// Items expected above half the limit: each goes alone, in its own batch.
    pub alone: Vec<String>,
    #[serde(skip_serializing_if = "BTreeMap::is_empty")]
    pub next_train: BTreeMap<String, Vec<String>>,
    pub reason: String,
}

/// Model cap (a): 10, 5 or 3 items.
pub fn model_cap(writer: &Writer) -> (usize, &'static str) {
    let strong = writer
        .aa_intelligence
        .is_some_and(|n| n.is_finite() && n >= STRONG_INTELLIGENCE);
    match (strong, writer.context_window.unwrap_or(0) >= LARGE_WINDOW) {
        (true, true) => (10, "strong writer, window of 400k or more"),
        (true, false) => (5, "strong writer, window below 400k"),
        _ => (3, "writer below the strong threshold"),
    }
}

fn median(values: &[u64]) -> Option<u64> {
    let mut sorted = values.to_vec();
    sorted.sort_unstable();
    match sorted.len() {
        0 => None,
        n if n % 2 == 1 => Some(sorted[n / 2]),
        n => Some((sorted[n / 2 - 1] + sorted[n / 2]) / 2),
    }
}

/// Expected diff bytes: the item's estimate, else the median of merged items of its kind, else
/// the median of all merged items, else `UNKNOWN_ITEM_BYTES`.
pub fn expected_bytes(item: &Item, merged: &BTreeMap<String, Vec<u64>>) -> u64 {
    item.expected_bytes
        .or_else(|| merged.get(&item.kind).and_then(|v| median(v)))
        .or_else(|| median(&merged.values().flatten().copied().collect::<Vec<_>>()))
        .unwrap_or(UNKNOWN_ITEM_BYTES)
}

/// Feedback bound (c): the last batch's count moved by one, or no bound without a last batch.
pub fn feedback_bound(last: Option<&Last>) -> (usize, &'static str) {
    match last {
        None => (usize::MAX, "no earlier batch"),
        Some(Last { count, outcome }) => match outcome {
            Outcome::Skipped => (
                count.saturating_sub(1).max(1),
                "last batch had skipped items: one fewer",
            ),
            Outcome::HighSurvived => (
                count.saturating_sub(1).max(1),
                "last batch kept a HIGH over two rounds: one fewer",
            ),
            Outcome::AcceptedRound1 => (count + 1, "last batch accepted in round 1: one more"),
            Outcome::Neutral => (*count, "last batch neutral: same count"),
        },
    }
}

/// Docs and changelog fragments do not couple delivery of source changes.
pub(crate) fn part_source_path(path: &str) -> bool {
    !path.starts_with("changelog.d/") && !path.starts_with("docs/")
}

fn part_paths(paths: &[String]) -> impl Iterator<Item = &str> {
    paths
        .iter()
        .map(|path| repo_relative(path))
        .filter(|path| !path.is_empty() && part_source_path(path))
}

/// The batch to brief: how many of `input.items`, in the given order, and why.
pub fn plan(input: &Input) -> Plan {
    let limit = input.limit_bytes.unwrap_or(DEFAULT_LIMIT_BYTES);
    let target = limit * TARGET_PERCENT / 100;
    let (cap, cap_why) = model_cap(&input.writer);
    let (feedback, feedback_why) = feedback_bound(input.last.as_ref());
    let bound = cap.min(feedback);
    let (mut keys, mut alone, mut bytes) = (Vec::new(), Vec::new(), 0u64);
    let mut fit_stop = None;
    let prior_files: BTreeSet<_> = part_paths(&input.prior_part_files).collect();
    let mut next_train = BTreeMap::new();
    let eligible = input.items.iter().filter(|item| {
        let overlap: BTreeSet<_> = part_paths(&item.files)
            .filter(|path| prior_files.iter().any(|prior| paths_overlap(path, prior)))
            .map(str::to_owned)
            .collect();
        if overlap.is_empty() {
            true
        } else {
            next_train.insert(item.key.clone(), overlap.into_iter().collect::<Vec<_>>());
            false
        }
    });
    // Evaluate every item so deferrals are reported even when this part is already full.
    let eligible: Vec<_> = eligible.collect();
    for (index, item) in eligible.into_iter().enumerate() {
        let expected = expected_bytes(item, &input.merged_bytes);
        if expected > limit / 2 {
            if index == 0 {
                // The first item is too big to share a batch: it is the batch.
                keys.push(item.key.clone());
                bytes = expected;
                break;
            }
            alone.push(item.key.clone());
            continue;
        }
        if keys.len() >= bound {
            break;
        }
        if bytes + expected > target {
            fit_stop = Some(item.key.clone());
            break;
        }
        bytes += expected;
        keys.push(item.key.clone());
    }
    let count = keys.len();
    let reason = format!(
        "{count} item(s): model cap {cap} ({cap_why}); feedback {} ({feedback_why}); fit {bytes} of {target} target bytes (limit {limit}){}{}",
        if feedback == usize::MAX { "none".to_string() } else { feedback.to_string() },
        fit_stop.map_or(String::new(), |k| format!(", {k} would pass the target")),
        if alone.is_empty() { String::new() } else { format!("; alone: {}", alone.join(" ")) },
    );
    let mut reason = reason;
    for (key, paths) in &next_train {
        reason.push_str(&format!("; next train: {key} shares {}", paths.join(", ")));
    }
    Plan {
        count,
        keys,
        alone,
        next_train,
        reason,
    }
}

/// Item keys name the items of the brief: a repeated key would put one item's text twice.
fn check_unique_keys(input: &Input) -> Result<(), String> {
    let mut seen = std::collections::BTreeSet::new();
    match input.items.iter().find(|i| !seen.insert(i.key.as_str())) {
        Some(item) => Err(format!("item key {} is listed twice", item.key)),
        None => Ok(()),
    }
}

fn read_design(input: &Input, item: &Item, reference: &str) -> Result<String, String> {
    let invalid = || format!("item {} has unreadable design_ref {reference}", item.key);
    let (repository, path) = reference
        .split_once(':')
        .or_else(|| {
            item.key
                .split_once(':')
                .map(|(project, _)| (project, reference))
        })
        .ok_or_else(invalid)?;
    let root = input
        .design_roots
        .get(repository)
        .ok_or_else(|| format!("{}: no design root configured for {repository}", invalid()))?;
    let relative = Path::new(path);
    if path.is_empty()
        || path.contains(['\\', ':'])
        || !relative
            .components()
            .all(|part| matches!(part, Component::Normal(_)))
    {
        return Err(invalid());
    }
    let root = root.canonicalize().map_err(|_| invalid())?;
    let file = root.join(relative).canonicalize().map_err(|_| invalid())?;
    if !file.starts_with(&root) || !file.is_file() {
        return Err(invalid());
    }
    std::fs::read_to_string(file).map_err(|_| invalid())
}

/// The batch brief of the plan: a size block (how many of how many items and why, what waits
/// for the next batch), the selected items in order with their text, and the run values. The
/// `# Items` and `# Run values` headings are the ones the spawner recognises as a batch brief.
pub fn brief(input: &Input, plan: &Plan) -> Result<String, String> {
    check_unique_keys(input)?;
    let mut out = format!(
        "# Batch size\n{} of {} item(s): {}\n",
        plan.count,
        input.items.len(),
        plan.reason
    );
    let waiting: Vec<&str> = input
        .items
        .iter()
        .map(|i| i.key.as_str())
        .filter(|k| !plan.keys.iter().any(|s| s == k) && !plan.next_train.contains_key(*k))
        .collect();
    if !waiting.is_empty() {
        out.push_str(&format!("Left for the next batch: {}\n", waiting.join(" ")));
    }
    out.push_str("\n# Items\n");
    for key in &plan.keys {
        let missing_text = || format!("item {key} has no text for the brief");
        let item = input
            .items
            .iter()
            .find(|i| &i.key == key)
            .ok_or_else(missing_text)?;
        let text = item
            .text
            .as_deref()
            .map(str::trim)
            .filter(|t| !t.is_empty())
            .ok_or_else(missing_text)?;
        out.push_str(&format!("\n## {key}\n{text}\n"));
        if let Some(reference) = item.design_ref.as_deref() {
            let design = read_design(input, item, reference)?;
            let fence = "`".repeat(
                3 + design
                    .split(|ch| ch != '`')
                    .map(str::len)
                    .max()
                    .unwrap_or(0),
            );
            out.push_str(&format!(
                "\nDesign input (`{reference}`)\n{fence}\n{design}"
            ));
            if !design.ends_with('\n') {
                out.push('\n');
            }
            out.push_str(&format!("{fence}\n"));
        }
    }
    out.push_str("\n# Run values\n");
    for (name, value) in &input.run_values {
        out.push_str(&format!("{name}: {value}\n"));
    }
    Ok(out)
}

/// `plangraph batch-size INPUT.json [--brief]`: print the plan as JSON, or with `--brief` the
/// batch brief that carries exactly the selected items and the reason.
pub fn run(args: &[String]) -> i32 {
    execute(args, &mut std::io::stdout())
}

fn execute(args: &[String], out: &mut dyn std::io::Write) -> i32 {
    let (path, as_brief) = match args {
        [path] => (path, false),
        [path, flag] if flag == "--brief" => (path, true),
        _ => {
            eprintln!("usage: plangraph batch-size INPUT.json [--brief]");
            return 2;
        }
    };
    let parsed = std::fs::read(path)
        .map_err(|e| e.to_string())
        .and_then(|b| serde_json::from_slice::<Input>(&b).map_err(|e| e.to_string()));
    let result = parsed.and_then(|input| {
        check_unique_keys(&input)?;
        let plan = plan(&input);
        if as_brief {
            brief(&input, &plan)
        } else {
            Ok(serde_json::to_string(&plan).unwrap_or_default() + "\n")
        }
    });
    match result {
        Ok(text) => match out.write_all(text.as_bytes()).and_then(|()| out.flush()) {
            Ok(()) => 0,
            Err(error) => {
                eprintln!("batch-size: output not written: {error}");
                2
            }
        },
        Err(error) => {
            eprintln!("batch-size: {error}");
            2
        }
    }
}
