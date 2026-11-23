//! The batch writer input shared by the fleet Claude launcher and native Codex.

use serde::Deserialize;
use std::{borrow::Cow, fmt::Write};

const PREFIX: &str = "PLANGRAPH-BATCH-INPUT-V1\n";

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct BatchInput {
    items: Vec<Item>,
    run: RunValues,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Item {
    key: String,
    title: String,
    brief: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RunValues {
    name: String,
    worktree: String,
    base: String,
    writer_family: String,
    status_file: String,
    fast_checks: Vec<String>,
    review_cmd: String,
    review_wait_seconds: u64,
    round_cap: u8,
    trailers: Vec<String>,
}

fn single_line(value: &str) -> bool {
    !value.trim().is_empty() && !value.chars().any(char::is_control)
}

impl RunValues {
    fn render(self, brief: &mut String) -> Result<(), String> {
        let fields = [
            ("NAME", self.name),
            ("WORKTREE", self.worktree),
            ("BASE", self.base),
            ("WRITER_FAMILY", self.writer_family),
            ("STATUS_FILE", self.status_file),
        ];
        if fields.iter().any(|(_, value)| !single_line(value))
            || !single_line(&self.review_cmd)
            || self.review_wait_seconds == 0
            || !(1..=8).contains(&self.round_cap)
            || self.fast_checks.is_empty()
            || self.trailers.is_empty()
            || self
                .fast_checks
                .iter()
                .chain(&self.trailers)
                .any(|value| !single_line(value))
        {
            return Err("invalid batch writer Run values".into());
        }
        brief.push_str("# Run values\n");
        for (label, value) in fields {
            writeln!(brief, "{label}: {value}").unwrap();
        }
        brief.push_str("FAST_CHECKS:\n");
        for check in self.fast_checks {
            writeln!(brief, "- {check}").unwrap();
        }
        writeln!(
            brief,
            "REVIEW_CMD: {}\nREVIEW_WAIT_SECONDS: {}\nROUND_CAP: {}\nTrailers:",
            self.review_cmd, self.review_wait_seconds, self.round_cap
        )
        .unwrap();
        for trailer in self.trailers {
            writeln!(brief, "{trailer}").unwrap();
        }
        Ok(())
    }
}

/// Render a versioned batch input in the prompt transport. Plain briefs pass through.
pub fn render(text: &str) -> Result<Cow<'_, str>, String> {
    let payload = text.strip_prefix(PREFIX).or_else(|| {
        text.strip_prefix(PREFIX.trim_end_matches('\n'))?
            .strip_prefix("\r\n")
    });
    let Some(payload) = payload else {
        return Ok(Cow::Borrowed(text));
    };
    let BatchInput { items, run } = serde_json::from_str(payload)
        .map_err(|err| format!("invalid batch writer input: {err}"))?;
    if items.is_empty() {
        return Err("batch writer input requires at least one item".into());
    }
    let mut rendered = "# Batch writer brief\n\n# Items\n".to_owned();
    for item in items {
        if !single_line(&item.key) || !single_line(&item.title) || item.brief.trim().is_empty() {
            return Err("batch writer input requires an item key, title and brief".into());
        }
        writeln!(
            rendered,
            "\n### {} | {}\n{}",
            item.key,
            item.title,
            item.brief.trim_end()
        )
        .unwrap();
    }
    rendered.push('\n');
    run.render(&mut rendered)?;
    Ok(Cow::Owned(rendered))
}
