//! A part joins a train only with a green full CI run of its own lane (PART-CI-BEFORE-TRAIN).
//!
//! The run is the full workflow dispatched on the part's lane at a base equal to a current main; its
//! record is `{pr, head, run_id, base, workflow, conclusion}`. The train builder admits a part only when a
//! record names exactly the part's head, the full workflow, a `success` conclusion, and a base that
//! contains the previous train (so the run is not older than the last train of main) and lies under the
//! part's head. A red or missing run sends the part back to its writer with the run id; the train run
//! stays the integration check.
use super::git;
use super::train::PartIn;
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::path::Path;

/// The workflow whose full run counts (the jobs of `tests.yml`).
pub const FULL_WORKFLOW: &str = ".github/workflows/tests.yml";

/// The full CI run of one part's lane.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct PartCi {
    pub pr: u64,
    pub head: String,
    pub run_id: String,
    /// The commit of main the lane was rebased on when the run started.
    pub base: String,
    pub workflow: String,
    pub conclusion: String,
}

fn refusal(part: &PartIn, why: String) -> String {
    format!("part_ci: part #{} head {}: {why}", part.pr, part.head)
}

/// One refusal per part that may not join: no run, a run of another head or workflow, a red run, or a run
/// at a base older than `previous_train` (the merge commit of the last train on main).
pub fn refusals(
    repo: &Path,
    parts: &[PartIn],
    runs: &[PartCi],
    previous_train: &str,
) -> Result<Vec<String>, String> {
    let mut out = Vec::new();
    for part in parts {
        let Some(run) = runs.iter().find(|r| r.pr == part.pr && r.head == part.head) else {
            out.push(refusal(part, "no full CI run for this head".into()));
            continue;
        };
        if run.workflow != FULL_WORKFLOW || run.run_id.is_empty() {
            out.push(refusal(
                part,
                format!("run {} is not a run of {FULL_WORKFLOW}", run.run_id),
            ));
        } else if run.conclusion != "success" {
            out.push(refusal(
                part,
                format!(
                    "run {} concluded {}: back to its writer with the failing job log",
                    run.run_id, run.conclusion
                ),
            ));
        } else if !git::is_ancestor(repo, previous_train, &run.base)? {
            out.push(refusal(
                part,
                format!(
                    "run {} is at base {} older than the previous train {previous_train}: rebase and run again",
                    run.run_id, run.base
                ),
            ));
        } else if !git::is_ancestor(repo, &run.base, &part.head)? {
            out.push(refusal(
                part,
                format!(
                    "run {} base {} is not under the part head",
                    run.run_id, run.base
                ),
            ));
        }
    }
    Ok(out)
}

/// Admits the parts or refuses all the offending ones at once.
pub fn require(
    repo: &Path,
    parts: &[PartIn],
    runs: &[PartCi],
    previous_train: &str,
) -> Result<(), String> {
    let found = refusals(repo, parts, runs, previous_train)?;
    if found.is_empty() {
        Ok(())
    } else {
        Err(found.join("\n"))
    }
}

/// The runs that admitted `parts`, in part order.
fn accepted(parts: &[PartIn], runs: &[PartCi]) -> Vec<PartCi> {
    parts
        .iter()
        .filter_map(|p| runs.iter().find(|r| r.pr == p.pr && r.head == p.head))
        .cloned()
        .collect()
}

/// The gate of `land-train build`: `--part-ci FILE` (JSON array of runs) and `--previous-train SHA` are
/// required, so a train cannot be built without the part runs. The accepted runs are written to
/// `<out>/<name>.part-ci.json` next to the manifest, so each part's full-CI run id can be read back.
pub(super) fn gate_cli(
    flags: &BTreeMap<String, String>,
    repo: &Path,
    parts: &[PartIn],
    name: &str,
) -> Result<(), String> {
    let need = |k: &str| flags.get(k).ok_or_else(|| format!("missing --{k}"));
    let file = need("part-ci")?;
    let runs: Vec<PartCi> =
        serde_json::from_slice(&std::fs::read(file).map_err(|e| format!("{file}: {e}"))?)
            .map_err(|e| format!("{file}: {e}"))?;
    require(repo, parts, &runs, need("previous-train")?)?;
    let out = Path::new(flags.get("out").map_or(".", String::as_str));
    std::fs::create_dir_all(out).map_err(|e| e.to_string())?;
    let record = serde_json::to_vec_pretty(&accepted(parts, &runs)).map_err(|e| e.to_string())?;
    let file = out.join(format!("{name}.part-ci.json"));
    std::fs::write(&file, record).map_err(|e| format!("{}: {e}", file.display()))
}
