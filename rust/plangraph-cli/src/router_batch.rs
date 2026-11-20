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

/// One part of a batch PR: a writer branch that was merged into the batch branch.
#[derive(Debug, Deserialize)]
pub struct Part {
    pub name: String,
    pub head: String,
    pub ci: String,
    pub review_file: String,
    pub writer_family: String,
    pub reviewer_family: String,
}

#[derive(Debug, Deserialize)]
pub struct GoInput {
    pub decision: String,
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

/// The one GO row of a batch PR with the per-part evidence table. Refuses, naming the part,
/// when a head is not 40 hex, CI is not green, the review file is missing or the reviewer
/// family is the writer's.
pub fn go_row(input: &GoInput) -> Result<String, String> {
    if !decision_id(&input.decision) || !sha40(&input.head) || input.parts.is_empty() {
        return Err("go-row needs a D-id, a 40-hex batch head and at least one part".into());
    }
    let mut table = Vec::new();
    for part in &input.parts {
        let why = if !token(&part.name) {
            Some("name")
        } else if !sha40(&part.head) {
            Some("head is not 40 hex")
        } else if part.ci != "green" {
            Some("CI is not green")
        } else if !token(&part.review_file) {
            Some("review file missing")
        } else if !token(&part.writer_family) || !token(&part.reviewer_family) {
            Some("family missing")
        } else if part.writer_family == part.reviewer_family {
            Some("reviewer family is the writer family")
        } else {
            None
        };
        if let Some(why) = why {
            return Err(format!("part {}: {why}", part.name));
        }
        table.push(format!(
            "{} head {} ci {} review {} writer {} reviewer {}",
            part.name,
            part.head,
            part.ci,
            part.review_file,
            part.writer_family,
            part.reviewer_family
        ));
    }
    Ok(format!(
        "| {} | GO #{} GO-HEAD {} | parts {}: {} |",
        input.decision,
        input.pr,
        input.head,
        input.parts.len(),
        table.join("; ")
    ))
}

/// Rows for several lanes in one row file, one line per grant, in input order.
pub fn grant_rows(input: &GrantInput) -> Result<String, String> {
    if !decision_id(&input.decision) || input.grants.is_empty() {
        return Err("grant-rows needs a D-id and at least one grant".into());
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
            "| {} | GRANT {} to {} scope {} |\n",
            input.decision, grant.lane, grant.grantee, grant.scope
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

/// `plangraph router (go-row|grant-rows|ask-group) INPUT.json [OUT]`.
pub fn run(args: &[String]) -> i32 {
    let (Some(verb), Some(input)) = (args.first(), args.get(1)) else {
        eprintln!("usage: plangraph router go-row|grant-rows|ask-group INPUT.json [OUT]");
        return 2;
    };
    let text = match verb.as_str() {
        "go-row" => read(input)
            .and_then(|i: GoInput| go_row(&i))
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
