//! A train part needs a counted review ACCEPT whose `HEAD:` line is the part head (rule 3.63 (1)).
//!
//! The ACCEPT files are the reviewer's answers in one directory. A writer status file never counts:
//! it has no `FILES READ:` line, and its `VERDICT:` line reports the writer, not a review.
use super::train::PartIn;
use pg_worker_core::writer_families::{python_command, writer_keys};
use std::path::{Path, PathBuf};

const MAX_FILE: u64 = 1 << 20;

/// What one review answer file says about the run that produced it and the head it names. A file whose
/// first line is the launcher's header owns that run id however broken the rest is; text that cannot be
/// read as one unambiguous answer is `ambiguous` and poisons its run at every head.
#[derive(Debug, PartialEq, Eq)]
pub struct Accept {
    /// The one sha every `HEAD:` line names; `None` when there is none, it is not a full sha or the lines conflict.
    pub head: Option<String>,
    /// The review leg named by the `[served by LEG via spawner run RUN, ...]` header, lowercased.
    pub leg: String,
    pub run_id: String,
    /// `FILES READ:` and `VERDICT: ACCEPT` as the last line, every `VERDICT:` line agreeing, one head, one
    /// header, completed: only such an answer can count.
    pub accepted: bool,
    /// The file repeats or conflicts on header, head or verdict lines, or its run did not complete, or it is
    /// too large or not text to read whole.
    pub ambiguous: bool,
}

/// The two OpenCode seats every head needs when no single-seat leg accepted it (rule 3.66 (f)): the
/// exact route leg ids of `REVIEW_REQUIRED` in `plangraph.planner.legs`.
const REQUIRED_PAIR: [&str; 2] = ["opencode-go-deepseek-v4-flash", "opencode-go-glm-5.3-flash"];
/// The exact legs whose one ACCEPT counts for the head alone, comma separated (rule 3.66 (f10) clause 8,
/// D-6804): Hydrogen (`REVIEW_HYDROGEN`), and the gpt high-effort leg under the conditions of `GPT_ALONE`.
pub const DEFAULT_SINGLE_SEAT: &str = "agy-gemini-4-hydrogen-high,cx-gpt-6.2-sol-high";
/// A single-seat leg that counts alone only when the launcher's metadata for its run says effort high,
/// decision D-6804 and every writer family of the head is known to the shared writer-family resolver and
/// differs from the reviewer leg's own family.
const GPT_ALONE_LEG: &str = "cx-gpt-6.2-sol-high";
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
    /// `None`: the writer-family resolver is the installed plangraph, loaded by an isolated interpreter
    /// (`-I`: no PYTHONPATH, no current directory) so the unverified `--repo` checkout never supplies it.
    /// `Some` names a checkout the caller itself trusts (tests).
    pub src_root: Option<PathBuf>,
}

impl AcceptGate {
    /// `meta` is a JSON object of run id to `SeatMeta`, written by the launcher of the review runs.
    pub fn read_meta(path: &Path) -> Result<std::collections::BTreeMap<String, SeatMeta>, String> {
        let text = std::fs::read_to_string(path)
            .map_err(|e| format!("seat meta {}: {e}", path.display()))?;
        serde_json::from_str(&text).map_err(|e| format!("seat meta {}: {e}", path.display()))
    }

    /// The interpreter that resolves writer families. Without a trusted `src_root` it is the interpreter
    /// named by `PG_SPAWND_PYTHON` run isolated, so only an installed plangraph can answer.
    pub fn python(&self) -> std::process::Command {
        let mut command = python_command(self.src_root.as_deref());
        if self.src_root.is_none() {
            command.arg("-I");
        }
        command
    }

    /// Whether a single-seat answer meets the extra conditions of its leg.
    fn alone_conditions_met(&self, accept: &Accept) -> bool {
        if accept.leg != GPT_ALONE_LEG {
            return true;
        }
        let Some(meta) = self.meta.get(&accept.run_id) else {
            return false;
        };
        let high = meta.effort.as_deref().map(str::to_ascii_lowercase);
        if high.as_deref() != Some("high")
            || meta.effort_decision.as_deref() != Some(GPT_EFFORT_DECISION)
            || meta.writer_families.is_empty()
        {
            return false;
        }
        // The shared resolver names the reviewer's family and each writer family; a blank, unknown or
        // ambiguous entry resolves to nothing and refuses, as does the reviewer's own family.
        let mut names = vec![accept.leg.clone()];
        names.extend(meta.writer_families.iter().cloned());
        let python = || self.python();
        let Ok(keys) = writer_keys(&names, &python) else {
            return false;
        };
        let reviewer = keys.get(&accept.leg).cloned().flatten();
        reviewer.is_some()
            && meta.writer_families.iter().all(|family| {
                keys.get(family)
                    .cloned()
                    .flatten()
                    .is_some_and(|key| Some(&key) != reviewer.as_ref())
            })
    }
}

fn run_id_ok(id: &str) -> bool {
    (1..=80).contains(&id.len())
        && id
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_' || b == b'.')
}

/// `[served by LEG via spawner run RUN, status STATUS]`, matched as a whole line: (leg, run, completed).
fn header(line: &str) -> Option<(String, String, bool)> {
    let rest = line.trim().strip_prefix("[served by ")?.strip_suffix(']')?;
    let (leg, rest) = rest.split_once(" via spawner run ")?;
    let (run, status) = rest.split_once(", status ")?;
    (run_id_ok(run) && !leg.contains(' ') && !leg.is_empty()).then(|| {
        (
            leg.to_ascii_lowercase(),
            run.to_owned(),
            status == "completed",
        )
    })
}

/// `Some` for every file whose first non-empty line is the launcher's header, which is how a run id is
/// owned; `None` for any other text (it owns no run). The launcher writes the header as the first line, so
/// a second line of that form in the body, several or conflicting `HEAD:` or `VERDICT:` lines, a run that
/// did not complete or no single full `HEAD:` sha make the answer `ambiguous`, never absent.
pub fn parse(text: &str) -> Option<Accept> {
    let mut lines = text.lines().filter(|l| !l.trim().is_empty());
    let (leg, run_id, completed) = header(lines.next()?)?;
    let body: Vec<&str> = lines.collect();
    let repeated_header = body
        .iter()
        .any(|l| l.trim_start().starts_with("[served by "));
    let heads: Vec<&str> = body
        .iter()
        .filter_map(|l| l.trim().strip_prefix("HEAD:"))
        .map(str::trim)
        .collect();
    let head = heads.first().copied().filter(|h| {
        h.len() == 40
            && h.bytes().all(|b| b.is_ascii_hexdigit())
            && heads.iter().all(|other| other.eq_ignore_ascii_case(h))
    });
    let verdicts: Vec<&str> = body
        .iter()
        .filter_map(|l| l.trim().strip_prefix("VERDICT:"))
        .map(str::trim)
        .collect();
    let verdict_agrees = verdicts.windows(2).all(|pair| pair[0] == pair[1]);
    let last_is_accept = body.last().is_some_and(|l| l.trim() == "VERDICT: ACCEPT");
    let ambiguous = repeated_header || !completed || head.is_none() || !verdict_agrees;
    Some(Accept {
        head: head.map(str::to_ascii_lowercase),
        leg,
        run_id,
        accepted: !ambiguous && last_is_accept && body.iter().any(|l| l.starts_with("FILES READ:")),
        ambiguous,
    })
}

/// Every answer file of the directory. A file that is too large or not UTF-8 is still read (its first
/// `MAX_FILE` bytes, lossily) so that a launcher header at its top keeps owning its run; it is then
/// ambiguous. A file that cannot be opened at all refuses the whole check.
fn accepts(dir: &Path) -> Result<Vec<(PathBuf, Accept)>, String> {
    use std::io::Read;
    let mut found = Vec::new();
    let entries =
        std::fs::read_dir(dir).map_err(|e| format!("accept dir {}: {e}", dir.display()))?;
    for entry in entries {
        let path = entry.map_err(|e| e.to_string())?.path();
        if !std::fs::metadata(&path)
            .map(|m| m.is_file())
            .unwrap_or(false)
        {
            continue;
        }
        let mut bytes = Vec::new();
        std::fs::File::open(&path)
            .and_then(|f| f.take(MAX_FILE + 1).read_to_end(&mut bytes))
            .map_err(|e| format!("accept file {}: {e}", path.display()))?;
        let whole = bytes.len() as u64 <= MAX_FILE && std::str::from_utf8(&bytes).is_ok();
        if let Some(mut accept) = parse(&String::from_utf8_lossy(&bytes)) {
            if !whole {
                accept.ambiguous = true;
                accept.accepted = false;
            }
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
        // An ambiguous answer names no reliable head, so it claims its run at every head.
        let at_head: Vec<_> = found
            .iter()
            .filter(|(_, a)| a.ambiguous || a.head.as_deref() == Some(head.as_str()))
            .collect();
        let bound: Vec<_> = at_head
            .iter()
            .filter(|(_, a)| a.run_id == part.receipt.run_id)
            .collect();
        let run_of = |leg: &str| {
            at_head
                .iter()
                .filter(|(_, a)| a.accepted && a.leg == leg)
                .map(|(_, a)| a.run_id.as_str())
                .collect::<Vec<_>>()
        };
        let pair: Vec<_> = REQUIRED_PAIR.iter().map(|leg| run_of(leg)).collect();
        // Each pair run belongs to exactly one completed answer at the head, whatever its verdict or leg,
        // so a run that a REQUEST_CHANGES or another leg's answer also claims never counts.
        let unique = |run: &str| at_head.iter().filter(|(_, a)| a.run_id == run).count() == 1;
        let paired =
            pair.iter().all(|runs| runs.len() == 1 && unique(runs[0])) && pair[0][0] != pair[1][0];
        let counted = match bound.as_slice() {
            [one] if one.1.accepted => {
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
                            accept.head.as_deref().unwrap_or("no single head"),
                            accept.run_id,
                            accept.leg
                        )
                    })
                    .collect();
                let why = match bound.len() {
                    0 => "no ACCEPT at this head carries the receipt run",
                    1 if bound[0].1.ambiguous => "the answer of the receipt run is ambiguous (repeated or conflicting header, head or verdict lines, or not completed)",
                    1 if !bound[0].1.accepted => "the answer of the receipt run does not accept",
                    1 => "neither a single-seat leg meeting its conditions nor both required legs on two runs accepted it",
                    _ => "the receipt run appears in more than one answer at this head",
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
