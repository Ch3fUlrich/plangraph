//! Batch writer transport. The launch policy is typed separately from the requested rules.
use serde::{Deserialize, Serialize};
use std::{collections::BTreeSet, fmt::Write};

pub const PREFIX: &str = "PLANGRAPH-BATCH-INPUT-V2";
pub const LEGACY_PREFIX: &str = "PLANGRAPH-BATCH-INPUT-V1";
pub const RENDERED_MARKER: &str = "<!-- PLANGRAPH-BATCH-PROMPT-V2 -->";

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Item {
    pub key: String,
    pub title: String,
    pub description: String,
    pub done_criteria: String,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub files: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub context: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(tag = "owner", rename_all = "snake_case", deny_unknown_fields)]
pub enum Review {
    Driver {},
    Writer { command: String, wait_seconds: u64 },
}

#[derive(Clone, Debug, Deserialize, PartialEq, Eq, Serialize)]
#[serde(tag = "id", rename_all = "snake_case", deny_unknown_fields)]
pub enum Rule {
    AffectedTests { jobs: u16 },
    Preflight,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Eq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Policy {
    pub revision: String,
    pub rules: Vec<Rule>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RunValues {
    pub name: String,
    pub base: String,
    pub status_file: String,
    pub review: Review,
    pub policy_revision: String,
    pub rules: Vec<Rule>,
    pub trailers: Vec<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Input {
    pub items: Vec<Item>,
    pub run: RunValues,
    pub policy: Policy,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub context: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub pushed_fix: Option<PushedFix>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct PushedFix {
    pub reference: String,
    pub sha: String,
}

fn single_line(text: &str) -> bool {
    !text.trim().is_empty() && !text.chars().any(char::is_control)
}

fn atx(line: &str) -> Option<(usize, &str)> {
    let level = line.bytes().take_while(|b| *b == b'#').count();
    let title = line[level..].trim();
    ((1..=6).contains(&level)
        && (line.len() == level || line[level..].starts_with(char::is_whitespace)))
    .then_some((level, title))
}

fn item_text(text: &str) -> Result<(), &'static str> {
    if text.trim().is_empty() {
        return Err("missing text");
    }
    if text
        .chars()
        .any(|ch| ch.is_control() && !matches!(ch, '\n' | '\r' | '\t'))
    {
        return Err("control character in item text");
    }
    if text.match_indices("…[+").any(|(at, _)| {
        text[at..]
            .split_once(" chars]")
            .is_some_and(|(n, _)| n["…[+".len()..].parse::<usize>().is_ok())
    }) {
        return Err("truncated item text");
    }
    let mut open_heading = None;
    let mut fence = None;
    for line in text.lines().map(str::trim) {
        if let Some((marker, width)) = fence {
            if line.len() >= width && line.chars().all(|c| c == marker) {
                fence = None;
            }
            continue;
        }
        if let Some(marker @ ('`' | '~')) = line.chars().next() {
            let width = line.chars().take_while(|c| *c == marker).count();
            if width >= 3 {
                fence = Some((marker, width));
            }
        }
        if let Some((level, title)) = atx(line) {
            if title.trim_matches('#').trim().is_empty()
                || open_heading.is_some_and(|previous| previous >= level)
            {
                return Err("empty heading");
            }
            if level == 1 && matches!(title, "Items" | "Run values" | "Writer rules") {
                return Err("reserved heading");
            }
            open_heading = Some(level);
        } else if !line.is_empty() {
            open_heading = None;
        }
    }
    if open_heading.is_some() {
        return Err("empty heading");
    }
    Ok(())
}

impl Item {
    fn validate(&self) -> Result<(), String> {
        let bad = |reason| format!("invalid batch writer item {}: {reason}", self.key);
        if !single_line(&self.key)
            || self.key.split_whitespace().count() != 1
            || !single_line(&self.title)
            || self.files.iter().any(|file| !single_line(file))
        {
            return Err(bad("invalid key, title or file"));
        }
        for text in [&self.description, &self.done_criteria] {
            item_text(text).map_err(bad)?;
        }
        let mut sections = [false; 2];
        let mut active = None;
        for line in self.description.lines() {
            let (line, next) = if let Some(rest) = line.strip_prefix("Why:") {
                (rest, Some(0))
            } else if let Some(rest) = line.strip_prefix("How:") {
                (rest, Some(1))
            } else {
                (line, active)
            };
            active = next;
            if let Some(section) = active.filter(|_| !line.trim().is_empty()) {
                sections[section] = true;
            }
        }
        if !sections.iter().all(|present| *present) {
            return Err(bad("Why and How are required"));
        }
        if let Some(context) = &self.context {
            item_text(context).map_err(bad)?;
        }
        Ok(())
    }
}

impl RunValues {
    fn validate(&self, policy: &Policy) -> Result<(), String> {
        if !single_line(&policy.revision)
            || self.policy_revision != policy.revision
            || self.rules != policy.rules
        {
            return Err("stale batch writer run rules: differ from launch policy".into());
        }
        if [&self.name, &self.base, &self.status_file]
            .iter()
            .any(|text| !single_line(text))
            || self.base.starts_with('-')
            || !self
                .base
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"/._-".contains(&b))
            || self.trailers.is_empty()
            || self.trailers.iter().any(|line| {
                !single_line(line)
                    || !line.split_once(": ").is_some_and(|(name, value)| {
                        single_line(value)
                            && matches!(
                                name,
                                "Plan-Item"
                                    | "Writer"
                                    | "Harness"
                                    | "Changelog"
                                    | "Gate-Edit"
                                    | "Fails-at-Base"
                                    | "Dropped-Part"
                            )
                    })
            })
        {
            return Err("invalid batch writer run values".into());
        }
        let mut ids = BTreeSet::new();
        for rule in &self.rules {
            let id = match rule {
                Rule::AffectedTests { jobs: 0 } => {
                    return Err("invalid batch writer rule: affected_tests requires jobs".into())
                }
                Rule::AffectedTests { .. } => "affected_tests",
                Rule::Preflight => "preflight",
            };
            if !ids.insert(id) {
                return Err(format!("duplicate batch writer rule: {id}"));
            }
        }
        if let Review::Writer {
            command,
            wait_seconds,
        } = &self.review
        {
            if !single_line(command) || *wait_seconds == 0 {
                return Err("invalid writer-owned review command or wait".into());
            }
        }
        Ok(())
    }

    pub fn render(&self) -> Result<String, String> {
        let mut out = format!(
            "# Run values\nNAME: {}\nBASE: {}\nSTATUS_FILE: {}\n",
            self.name, self.base, self.status_file
        );
        if !self.rules.is_empty() {
            out.push_str("Checks:\n");
        }
        for rule in &self.rules {
            match rule {
                Rule::AffectedTests { jobs } => writeln!(out, "- python3 tools/affected_tests.py --base {} --head HEAD --local --files-only --run -n {jobs}", self.base),
                Rule::Preflight => writeln!(out, "- python3 tools/preflight.py --base {} --head HEAD --lane", self.base),
            }.unwrap();
        }
        match &self.review {
            Review::Driver {} => {
                out.push_str("Review: the driver runs fast checks and review after your turn.\n")
            }
            Review::Writer {
                command,
                wait_seconds,
            } => writeln!(
                out,
                "Review: run `{command}` on the checked head; wait at most {wait_seconds} seconds."
            )
            .unwrap(),
        }
        out.push_str("Trailers:\n");
        for trailer in &self.trailers {
            writeln!(out, "{trailer}").unwrap();
        }
        if out.split_whitespace().count() > 150 {
            return Err("batch writer run values exceed 150 words".into());
        }
        Ok(out)
    }
}

impl Input {
    pub fn decode(text: &str) -> Result<Option<Self>, String> {
        if text.lines().take(4).any(|line| line == RENDERED_MARKER) {
            return Err("forward a typed batch input, not a pre-rendered batch prompt".into());
        }
        let Some((version, body)) = text.split_once('\n') else {
            return if text == PREFIX {
                Err("missing batch writer payload".into())
            } else {
                Ok(None)
            };
        };
        let version = version.trim_end_matches('\r');
        if version != PREFIX {
            return if version.starts_with("PLANGRAPH-BATCH-INPUT-") && version != LEGACY_PREFIX {
                Err("unknown batch writer input version".into())
            } else {
                Ok(None)
            };
        }
        let input: Self =
            serde_json::from_str(body).map_err(|e| format!("invalid batch writer input: {e}"))?;
        input.validate()?;
        Ok(Some(input))
    }

    pub fn encode(&self) -> Result<String, String> {
        self.validate()?;
        Ok(format!(
            "{PREFIX}\n{}",
            serde_json::to_string(self).map_err(|e| e.to_string())?
        ))
    }

    fn validate(&self) -> Result<(), String> {
        let mut keys = BTreeSet::new();
        if self.items.is_empty() {
            return Err("batch writer input requires at least one item".into());
        }
        for item in &self.items {
            item.validate()?;
            if !keys.insert(item.key.as_str()) {
                return Err(format!("duplicate batch writer item: {}", item.key));
            }
        }
        let assigned: Vec<_> = self
            .run
            .trailers
            .iter()
            .filter_map(|line| line.strip_prefix("Plan-Item: "))
            .collect();
        if assigned.len() != keys.len() || assigned.into_iter().collect::<BTreeSet<_>>() != keys {
            return Err("batch writer Plan-Item trailers differ from the assigned items".into());
        }
        if let Some(context) = &self.context {
            item_text(context)
                .map_err(|reason| format!("invalid batch writer context: {reason}"))?;
        }
        if let Some(fix) = &self.pushed_fix {
            if !matches!(self.run.review, Review::Writer { .. })
                || !fix.reference.starts_with("refs/heads/")
                || !single_line(&fix.reference)
                || fix.sha.len() != 40
                || !fix.sha.bytes().all(|b| b.is_ascii_hexdigit())
            {
                return Err("invalid pushed Claude fix round".into());
            }
        }
        self.run.validate(&self.policy)?;
        self.run.render()?;
        Ok(())
    }
}
