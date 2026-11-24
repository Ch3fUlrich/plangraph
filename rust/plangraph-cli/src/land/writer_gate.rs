//! A part joins a train only with a writer-host receipt (PART-CI-BEFORE-TRAIN, re-scoped by D-6773).
//!
//! No full CI runs per part: full CI runs once, on the train head. Before that a part must carry a review
//! ACCEPT on its exact head and a receipt of the fast gates and affected tests the writer host ran on that
//! head: `{pr, head, review_head, review_verdict, checks: [{command, exit_code}]}`. The train builder admits a
//! part only when its receipt names the part's head twice (receipt and review), the review is `ACCEPT`, and
//! every required check (the affected tests and the push preflight) was run with exit code 0. A part with
//! a missing or red receipt goes back to its writer; the train run stays the integration check.
use super::train::PartIn;
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::path::Path;

/// The commands every receipt must hold, matched by the start of the recorded command line.
pub const REQUIRED: [&str; 2] = [
    "python3 tools/affected_tests.py",
    "python3 tools/preflight.py",
];

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Check {
    pub command: String,
    pub exit_code: i32,
}

/// What the writer host recorded for one part head.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct WriterReceipt {
    pub pr: u64,
    pub head: String,
    /// The head the review judged.
    pub review_head: String,
    pub review_verdict: String,
    pub checks: Vec<Check>,
}

fn refusal(part: &PartIn, why: String) -> String {
    format!("writer_gate: part #{} head {}: {why}", part.pr, part.head)
}

fn why_not(part: &PartIn, r: &WriterReceipt) -> Option<String> {
    if r.review_head != part.head {
        return Some(format!(
            "the review judged {}, not this head",
            r.review_head
        ));
    }
    if r.review_verdict != "ACCEPT" {
        return Some(format!(
            "review verdict {} is not ACCEPT: back to its writer",
            r.review_verdict
        ));
    }
    for required in REQUIRED {
        let runs: Vec<&Check> = r
            .checks
            .iter()
            .filter(|c| c.command.starts_with(required))
            .collect();
        if runs.is_empty() {
            return Some(format!("no recorded run of `{required}`"));
        }
        if let Some(red) = runs.iter().find(|c| c.exit_code != 0) {
            return Some(format!(
                "`{}` exited {}: back to its writer",
                red.command, red.exit_code
            ));
        }
    }
    if let Some(red) = r.checks.iter().find(|c| c.exit_code != 0) {
        return Some(format!("`{}` exited {}", red.command, red.exit_code));
    }
    None
}

/// One refusal per part that may not join: no receipt for its exact head, a review of another head or
/// without ACCEPT, a required check missing, or any recorded check red.
pub fn refusals(parts: &[PartIn], receipts: &[WriterReceipt]) -> Vec<String> {
    parts
        .iter()
        .filter_map(|part| {
            match receipts
                .iter()
                .find(|r| r.pr == part.pr && r.head == part.head)
            {
                None => Some(refusal(part, "no writer-host receipt for this head".into())),
                Some(r) => why_not(part, r).map(|why| refusal(part, why)),
            }
        })
        .collect()
}

/// Admits the parts or refuses all the offending ones at once.
pub fn require(parts: &[PartIn], receipts: &[WriterReceipt]) -> Result<(), String> {
    let found = refusals(parts, receipts);
    if found.is_empty() {
        Ok(())
    } else {
        Err(found.join("\n"))
    }
}

/// The receipts that admitted `parts`, in part order.
fn accepted(parts: &[PartIn], receipts: &[WriterReceipt]) -> Vec<WriterReceipt> {
    parts
        .iter()
        .filter_map(|p| receipts.iter().find(|r| r.pr == p.pr && r.head == p.head))
        .cloned()
        .collect()
}

/// The gate of `land-train build`: `--writer-receipts FILE` (JSON array of receipts) is required, so a train
/// cannot be built without them. The accepted receipts are written to `<out>/<name>.writer-receipts.json`
/// next to the manifest.
pub(super) fn gate_cli(
    flags: &BTreeMap<String, String>,
    parts: &[PartIn],
    name: &str,
) -> Result<(), String> {
    let file = flags
        .get("writer-receipts")
        .ok_or("missing --writer-receipts")?;
    let receipts: Vec<WriterReceipt> =
        serde_json::from_slice(&std::fs::read(file).map_err(|e| format!("{file}: {e}"))?)
            .map_err(|e| format!("{file}: {e}"))?;
    require(parts, &receipts)?;
    let out = Path::new(flags.get("out").map_or(".", String::as_str));
    std::fs::create_dir_all(out).map_err(|e| e.to_string())?;
    let record =
        serde_json::to_vec_pretty(&accepted(parts, &receipts)).map_err(|e| e.to_string())?;
    let file = out.join(format!("{name}.writer-receipts.json"));
    std::fs::write(&file, record).map_err(|e| format!("{}: {e}", file.display()))
}
