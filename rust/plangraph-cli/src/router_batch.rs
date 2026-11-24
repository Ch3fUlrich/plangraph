//! Router batch mode: one command for a batch PR's GO row, one row file for several grants and
//! one grouped ask, so the router works a batch in one step instead of one script per item.
//!
//! `plangraph router go-row|grant-rows|ask-group INPUT.json [OUT]` print (or write) the text.
//! The keeper plan batch is `tools/apply_batch.py` (dry run, atomic apply, every refused entry
//! listed); the procedure that ties these together is the batch section of
//! `skills/router-core/SKILL.md`.
use serde::Deserialize;
use std::path::Path;

#[cfg(test)]
#[path = "router_batch_tests.rs"]
mod tests;

/// One review seat of a part (rule 3.66 (f)): the review leg that ran, its review file and
/// reviewer family.
#[derive(Debug, Deserialize)]
pub struct Seat {
    /// The route leg id of the review (`opencode-go-glm-5.3-flash`, ...).
    pub leg: String,
    pub review_file: String,
    pub reviewer_family: String,
}

/// One part of a batch PR: a writer branch that was merged into the batch branch.
#[derive(Debug, Deserialize)]
pub struct Part {
    pub name: String,
    pub head: String,
    pub ci: String,
    pub writer_family: String,
    /// The part's counted seats: one smart seat, or the weak pair when no smart leg served (rule 3.66
    /// (f10)); every seat must ACCEPT the part head with FILES READ.
    pub seats: Vec<Seat>,
}

/// Seats a part needs without a smart seat (D-6652, rule 3.66 (f)): the weak pair.
const REQUIRED_SEATS: usize = 2;
/// The smart review legs (`REVIEW_SMART` in `plangraph.planner.legs`): one ACCEPT of any of them is
/// the part's one counted seat (rule 3.66 (f10)).
const SMART_LEGS: [&str; 4] = [
    "agy-gemini-4-hydrogen-high",
    "agy-gemini-4-argon-low",
    "agy-gemini-4-argon-medium",
    "claude-sonnet-5-5-high",
];
/// The two required OpenCode review legs with their model family (`REVIEW_REQUIRED` in
/// `plangraph.planner.legs`). Both must be among a part's seats, except a leg whose family wrote
/// part of the diff (a writer never reviews its own work; other seats then fill its slot).
const REQUIRED_LEGS: [(&str, &str); 2] = [
    ("opencode-go-deepseek-v4-flash", "deepseek"),
    ("opencode-go-glm-5.3-flash", "glm"),
];

#[derive(Debug, Deserialize)]
pub struct GoInput {
    pub decision: String,
    /// The keeper project of the batch PR; the decisions-log `project` column.
    pub project: String,
    pub pr: u64,
    /// Head of the batch PR; the keeper binds the GO to it.
    pub head: String,
    pub parts: Vec<Part>,
}

#[derive(Debug, Deserialize)]
pub struct Grant {
    pub lane: String,
    pub grantee: String,
    pub scope: String,
}

#[derive(Debug, Deserialize)]
pub struct GrantInput {
    pub decision: String,
    pub project: String,
    pub grants: Vec<Grant>,
}

#[derive(Debug, Deserialize)]
pub struct Ask {
    pub item: String,
    pub question: String,
}

#[derive(Debug, Deserialize)]
pub struct AskInput {
    pub to: String,
    pub topic: String,
    pub asks: Vec<Ask>,
}

fn sha40(value: &str) -> bool {
    value.len() == 40 && value.bytes().all(|b| b.is_ascii_hexdigit())
}
fn decision_id(value: &str) -> bool {
    value
        .strip_prefix("D-")
        .is_some_and(|n| !n.is_empty() && n.bytes().all(|b| b.is_ascii_digit()))
}
fn token(value: &str) -> bool {
    !value.is_empty() && !value.contains(|c: char| c.is_whitespace() || c == '|' || c == ';')
}

/// Why `text` is no accepted review of `head`: every `HEAD:` line names that head, there is at
/// least one, the last `VERDICT:` line is `ACCEPT` and a `FILES READ:` line names repository paths
/// (a review of the diff alone is no seat, D-6652).
fn review_refusal(text: &str, head: &str) -> Option<&'static str> {
    let heads: Vec<&str> = text
        .lines()
        .filter_map(|l| l.trim().strip_prefix("HEAD:"))
        .map(str::trim)
        .collect();
    if heads.is_empty() || heads.iter().any(|h| !h.eq_ignore_ascii_case(head)) {
        return Some("review file is not for this head");
    }
    let verdict = text
        .lines()
        .rev()
        .find_map(|l| l.trim().strip_prefix("VERDICT:"))
        .map(str::trim);
    if verdict != Some("ACCEPT") {
        return Some("review file is not an ACCEPT");
    }
    (!pg_worker_core::review_split::has_files_read(text)).then_some("review file has no FILES READ")
}

/// The one GO row of a batch PR in the decisions-log schema (`| ID | project | decision | by |
/// why |`): the decision cell holds the GO and its binding (`GO #PR GO-HEAD head`), the why
/// cell the per-part evidence table. Refuses, naming the part, when a head is not 40 hex, CI is
/// not green, the part has fewer than two review seats, a seat's review file is missing,
/// unreadable, not for the part's head, not an ACCEPT or without FILES READ, a reviewer family is
/// the writer's, or two seats share a review file or a keeper family. `read` returns the text of
/// a review file; `family` maps a family name to its keeper family (claude and anthropic are one,
/// gpt and openai are one), so spelling cannot make two seats look independent.
pub fn go_row(
    input: &GoInput,
    read: &dyn Fn(&str) -> Result<String, String>,
    family: &dyn Fn(&str) -> String,
) -> Result<String, String> {
    if !decision_id(&input.decision)
        || !token(&input.project)
        || !sha40(&input.head)
        || input.parts.is_empty()
    {
        return Err(
            "go-row needs a D-id, a project, a 40-hex batch head and at least one part".into(),
        );
    }
    let mut table = Vec::new();
    for part in &input.parts {
        let smart = part
            .seats
            .iter()
            .any(|seat| SMART_LEGS.contains(&seat.leg.as_str()));
        let why = if !token(&part.name) {
            Some("name")
        } else if !sha40(&part.head) {
            Some("head is not 40 hex")
        } else if part.ci != "green" {
            Some("CI is not green")
        } else if part.seats.is_empty() || (!smart && part.seats.len() < REQUIRED_SEATS) {
            Some("needs one smart review seat or two required review seats")
        } else if part.seats.iter().any(|seat| !token(&seat.leg)) {
            Some("review leg missing")
        } else if part.seats.iter().any(|seat| !token(&seat.review_file)) {
            Some("review file missing")
        } else if !token(&part.writer_family)
            || part.seats.iter().any(|seat| !token(&seat.reviewer_family))
        {
            Some("family missing")
        } else if part
            .seats
            .iter()
            .any(|seat| family(&seat.reviewer_family) == family(&part.writer_family))
        {
            Some("reviewer family is the writer family")
        } else if part.seats.iter().enumerate().any(|(i, a)| {
            part.seats[..i]
                .iter()
                .any(|b| family(&a.reviewer_family) == family(&b.reviewer_family))
        }) {
            Some("review seats share a family")
        } else if part.seats.iter().enumerate().any(|(i, a)| {
            part.seats[..i]
                .iter()
                .any(|b| a.review_file == b.review_file)
        }) {
            Some("review seats share a review file")
        } else if !smart
            && REQUIRED_LEGS.iter().any(|(leg, model_family)| {
                family(model_family) != family(&part.writer_family)
                    && !part.seats.iter().any(|seat| seat.leg == *leg)
            })
        {
            Some("required review seat missing (opencode-go deepseek and glm)")
        } else {
            None
        };
        if let Some(why) = why {
            return Err(format!("part {}: {why}", part.name));
        }
        let mut seats = Vec::new();
        for seat in &part.seats {
            let review = read(&seat.review_file)
                .map_err(|e| format!("part {}: review file unreadable: {e}", part.name))?;
            if let Some(why) = review_refusal(&review, &part.head) {
                return Err(format!("part {}: {why}", part.name));
            }
            seats.push(format!(
                "review {} leg {} reviewer {}",
                seat.review_file, seat.leg, seat.reviewer_family
            ));
        }
        table.push(format!(
            "{} head {} ci {} writer {} {}",
            part.name,
            part.head,
            part.ci,
            part.writer_family,
            seats.join(" ")
        ));
    }
    Ok(format!(
        "| {} | {} | GO #{} GO-HEAD {} | router | parts {}: {} |",
        input.decision,
        input.project,
        input.pr,
        input.head,
        input.parts.len(),
        table.join("; ")
    ))
}

/// Rows for several lanes in one row file, one line per grant, in input order.
pub fn grant_rows(input: &GrantInput) -> Result<String, String> {
    if !decision_id(&input.decision) || !token(&input.project) || input.grants.is_empty() {
        return Err("grant-rows needs a D-id, a project and at least one grant".into());
    }
    let mut seen = Vec::new();
    let mut out = String::new();
    for grant in &input.grants {
        if !token(&grant.lane) || !token(&grant.grantee) || !token(&grant.scope) {
            return Err(format!(
                "grant {}: lane, grantee and scope are single words",
                grant.lane
            ));
        }
        if seen.contains(&&grant.lane) {
            return Err(format!("grant {}: lane listed twice", grant.lane));
        }
        seen.push(&grant.lane);
        out.push_str(&format!(
            "| {} | {} | GRANT {} to {} scope {} | router | |\n",
            input.decision, input.project, grant.lane, grant.grantee, grant.scope
        ));
    }
    Ok(out)
}

/// One message to `to` that groups related asks, numbered, one line each.
pub fn ask_group(input: &AskInput) -> Result<String, String> {
    if !token(&input.to) || input.topic.trim().is_empty() || input.asks.is_empty() {
        return Err("ask-group needs a recipient, a topic and at least one ask".into());
    }
    let mut out = format!(
        "ASK-GROUP {} | {} | {} item(s)\n",
        input.to,
        input.topic.trim(),
        input.asks.len()
    );
    for (n, ask) in input.asks.iter().enumerate() {
        if ask.question.contains('\n') || ask.question.trim().is_empty() || !token(&ask.item) {
            return Err(format!(
                "ask {}: a one-line question and an item key",
                n + 1
            ));
        }
        out.push_str(&format!(
            "{}. {} | {}\n",
            n + 1,
            ask.item,
            ask.question.trim()
        ));
    }
    Ok(out)
}

fn read<T: for<'de> Deserialize<'de>>(path: &str) -> Result<T, String> {
    let bytes = std::fs::read(path).map_err(|e| format!("{path}: {e}"))?;
    serde_json::from_slice(&bytes).map_err(|e| format!("{path}: {e}"))
}

fn read_review(path: &str) -> Result<String, String> {
    let meta = std::fs::metadata(path).map_err(|e| format!("{path}: {e}"))?;
    if !meta.is_file() {
        return Err(format!("{path}: not a file"));
    }
    std::fs::read_to_string(path).map_err(|e| format!("{path}: {e}"))
}

/// The keeper family of every family name the input uses, from the Python family table.
fn keeper_family_table(
    input: &GoInput,
) -> Result<std::collections::HashMap<String, String>, String> {
    let mut names: Vec<String> = REQUIRED_LEGS
        .iter()
        .map(|(_, family)| family.to_string())
        .chain(input.parts.iter().flat_map(|p| {
            std::iter::once(p.writer_family.clone())
                .chain(p.seats.iter().map(|s| s.reviewer_family.clone()))
        }))
        .collect();
    names.sort();
    names.dedup();
    let root = std::env::current_dir().ok();
    pg_worker_core::writer_families::keeper_families(&names, &|| {
        pg_worker_core::writer_families::python_command(root.as_deref())
    })
}

/// `plangraph router (go-row|grant-rows|ask-group) INPUT.json [OUT]`.
pub fn run(args: &[String]) -> i32 {
    let (Some(verb), Some(input)) = (args.first(), args.get(1)) else {
        eprintln!("usage: plangraph router go-row|grant-rows|ask-group INPUT.json [OUT]");
        return 2;
    };
    let text = match verb.as_str() {
        "go-row" => read(input)
            .and_then(|i: GoInput| {
                let table = keeper_family_table(&i)?;
                go_row(&i, &read_review, &|name| {
                    table.get(name).cloned().unwrap_or_else(|| name.to_owned())
                })
            })
            .map(|t| t + "\n"),
        "grant-rows" => read(input).and_then(|i: GrantInput| grant_rows(&i)),
        "ask-group" => read(input).and_then(|i: AskInput| ask_group(&i)),
        _ => Err(format!("unknown router command {verb}")),
    };
    match text {
        Ok(text) => match args.get(2) {
            Some(out) => match std::fs::write(Path::new(out), &text) {
                Ok(()) => 0,
                Err(e) => {
                    eprintln!("router {verb}: {e}");
                    2
                }
            },
            None => {
                print!("{text}");
                0
            }
        },
        Err(reason) => {
            eprintln!("router {verb}: {reason}");
            1
        }
    }
}
