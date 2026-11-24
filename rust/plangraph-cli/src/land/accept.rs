//! A train part needs a counted review ACCEPT whose `HEAD:` line is the part head (rule 3.63 (1)).
//!
//! The ACCEPT files are the reviewer's answers in one directory. A writer status file never counts:
//! it has no `FILES READ:` line, and its `VERDICT:` line reports the writer, not a review.
use super::train::PartIn;
use std::path::{Path, PathBuf};

const MAX_FILE: u64 = 1 << 20;

/// What one review answer says about the head it names and the run that produced it.
#[derive(Debug, PartialEq, Eq)]
pub struct Accept {
    pub head: String,
    /// The review leg named by the `[served by LEG via spawner run RUN, ...]` header, lowercased.
    pub leg: String,
    pub run_id: String,
}

/// The two OpenCode seats every head needs when no single-seat leg accepted it (rule 3.66 (f)): the
/// exact route leg ids of `REVIEW_REQUIRED` in `plangraph.planner.legs`.
const REQUIRED_PAIR: [&str; 2] = ["opencode-go-deepseek-v4-flash", "opencode-go-glm-5.3-flash"];
/// The exact legs whose one ACCEPT counts for the head alone, comma separated (rule 3.66 (f10) clause 8,
/// D-6804): Hydrogen (`REVIEW_HYDROGEN`), and the gpt high-effort leg under the conditions of `GPT_ALONE`.
pub const DEFAULT_SINGLE_SEAT: &str = "agy-gemini-4-hydrogen-high,cx-gpt-6.2-sol-high";
/// A single-seat leg that counts alone only when the launcher's metadata for its run says effort high,
/// decision D-6804 and a writer family other than the reviewer's own.
const GPT_ALONE: (&str, [&str; 3]) = ("cx-gpt-6.2-sol-high", ["gpt", "openai", "codex"]);
const GPT_EFFORT_DECISION: &str = "D-6804";

/// What the orchestrator that launched a review run recorded about its card; never read from the answer.
#[derive(Clone, Debug, Default, serde::Deserialize)]
pub struct SeatMeta {
    #[serde(default)]
    pub effort: Option<String>,
    #[serde(default)]
    pub effort_decision: Option<String>,
    /// The model families that wrote the reviewed head.
    #[serde(default)]
    pub writer_families: Vec<String>,
}

/// Where the reviewers' answers are, which exact legs count alone, and the launcher metadata by run id.
#[derive(Clone, Debug)]
pub struct AcceptGate {
    pub dir: PathBuf,
    pub single_seat: Vec<String>,
    pub meta: std::collections::BTreeMap<String, SeatMeta>,
}

impl AcceptGate {
    /// `meta` is a JSON object of run id to `SeatMeta`, written by the launcher of the review runs.
    pub fn read_meta(path: &Path) -> Result<std::collections::BTreeMap<String, SeatMeta>, String> {
        let text = std::fs::read_to_string(path)
            .map_err(|e| format!("seat meta {}: {e}", path.display()))?;
        serde_json::from_str(&text).map_err(|e| format!("seat meta {}: {e}", path.display()))
    }

    /// Whether a single-seat answer meets the extra conditions of its leg.
    fn alone_conditions_met(&self, accept: &Accept) -> bool {
        if accept.leg != GPT_ALONE.0 {
            return true;
        }
        self.meta.get(&accept.run_id).is_some_and(|meta| {
            meta.effort
                .as_deref()
                .map(str::to_ascii_lowercase)
                .as_deref()
                == Some("high")
                && meta.effort_decision.as_deref() == Some(GPT_EFFORT_DECISION)
                && !meta.writer_families.is_empty()
                && !meta.writer_families.iter().any(|family| {
                    GPT_ALONE
                        .1
                        .contains(&family.trim().to_ascii_lowercase().as_str())
                })
        })
    }
}

fn run_id_ok(id: &str) -> bool {
    (1..=80).contains(&id.len())
        && id
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_' || b == b'.')
}

/// `[served by LEG via spawner run RUN, status completed]`
fn served_by(text: &str) -> Option<(String, String)> {
    text.lines().find_map(|line| {
        let rest = line.trim().strip_prefix("[served by ")?;
        let (leg, rest) = rest.split_once(" via spawner run ")?;
        let (run, status) = rest.split_once(", status ")?;
        (status.trim_end_matches(']') == "completed" && run_id_ok(run) && !leg.contains(' '))
            .then(|| (leg.to_ascii_lowercase(), run.to_owned()))
    })
}

/// `Some` only for a review answer that accepts: a completed `served by` header with its run id, a
/// `FILES READ:` line, a full `HEAD:` sha, and the last non-empty line `VERDICT: ACCEPT`.
pub fn parse(text: &str) -> Option<Accept> {
    let last = text.lines().rev().find(|l| !l.trim().is_empty())?;
    if last.trim() != "VERDICT: ACCEPT" || !text.lines().any(|l| l.starts_with("FILES READ:")) {
        return None;
    }
    let (leg, run_id) = served_by(text)?;
    let head = text
        .lines()
        .rev()
        .find_map(|l| l.trim().strip_prefix("HEAD:"))?
        .trim();
    let full = head.len() == 40 && head.bytes().all(|b| b.is_ascii_hexdigit());
    full.then(|| Accept {
        head: head.to_ascii_lowercase(),
        leg,
        run_id,
    })
}

fn accepts(dir: &Path) -> Result<Vec<(PathBuf, Accept)>, String> {
    let mut found = Vec::new();
    let entries =
        std::fs::read_dir(dir).map_err(|e| format!("accept dir {}: {e}", dir.display()))?;
    for entry in entries {
        let path = entry.map_err(|e| e.to_string())?.path();
        let small = std::fs::metadata(&path)
            .map(|m| m.is_file() && m.len() <= MAX_FILE)
            .unwrap_or(false);
        if let Some(accept) = small
            .then(|| std::fs::read_to_string(&path).ok())
            .flatten()
            .and_then(|text| parse(&text))
        {
            found.push((path, accept));
        }
    }
    found.sort_by(|a, b| a.0.cmp(&b.0));
    Ok(found)
}

/// Every part has counted ACCEPT evidence at its exact head; returns one line per part for the PR body.
///
/// Counted means: the part's registrar receipt names a run, exactly one ACCEPT file at the part head
/// carries that run id, and the head is covered either by that answer's leg being one of `single_seat`
/// or by both required OpenCode legs (exact ids) accepting the head on two different runs. Leg ids are
/// compared whole, never as substrings, and a run id that appears in two answers at the head is refused.
/// The error names the part, its head and the directory, and lists the ACCEPT files found.
pub fn require(gate: &AcceptGate, parts: &[PartIn]) -> Result<Vec<String>, String> {
    let (dir, single_seat) = (gate.dir.as_path(), &gate.single_seat);
    let found = accepts(dir)?;
    let mut lines = Vec::new();
    for part in parts {
        let head = part.head.to_ascii_lowercase();
        let at_head: Vec<_> = found.iter().filter(|(_, a)| a.head == head).collect();
        let bound: Vec<_> = at_head
            .iter()
            .filter(|(_, a)| a.run_id == part.receipt.run_id)
            .collect();
        let run_of = |leg: &str| {
            at_head
                .iter()
                .filter(|(_, a)| a.leg == leg)
                .map(|(_, a)| a.run_id.as_str())
                .collect::<Vec<_>>()
        };
        let pair: Vec<_> = REQUIRED_PAIR.iter().map(|leg| run_of(leg)).collect();
        // Each pair run belongs to exactly one answer at the head, so a run claimed by a second answer
        // (of any leg) never counts.
        let unique = |run: &str| at_head.iter().filter(|(_, a)| a.run_id == run).count() == 1;
        let paired =
            pair.iter().all(|runs| runs.len() == 1 && unique(runs[0])) && pair[0][0] != pair[1][0];
        let counted = match bound.as_slice() {
            [one] => {
                single_seat.contains(&one.1.leg) && gate.alone_conditions_met(&one.1)
                    || (paired && pair.iter().any(|runs| runs[0] == one.1.run_id))
            }
            _ => false,
        };
        match bound.first() {
            Some((path, _)) if counted => lines.push(format!(
                "accept #{} head {} run {} file {}",
                part.pr,
                part.head,
                part.receipt.run_id,
                path.file_name().unwrap_or_default().to_string_lossy()
            )),
            _ => {
                let seen: Vec<String> = found
                    .iter()
                    .map(|(path, accept)| {
                        format!(
                            "{} on {} run {} leg {}",
                            path.file_name().unwrap_or_default().to_string_lossy(),
                            accept.head,
                            accept.run_id,
                            accept.leg
                        )
                    })
                    .collect();
                let why = match bound.len() {
                    0 => "no ACCEPT at this head carries the receipt run",
                    1 => "neither a single-seat leg meeting its conditions nor both required legs on two runs accepted it",
                    _ => "the receipt run appears in more than one ACCEPT at this head",
                };
                return Err(format!(
                    "accept: part #{} head {} is not counted: {why} (receipt run {}, dir {}, ACCEPT files found: {})",
                    part.pr,
                    part.head,
                    part.receipt.run_id,
                    dir.display(),
                    if seen.is_empty() { "none".into() } else { seen.join(", ") }
                ));
            }
        }
    }
    Ok(lines)
}
