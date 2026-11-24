//! The batch writer input shared by the fleet Claude launcher and native Codex.

use serde::Deserialize;
use std::{borrow::Cow, fmt::Write};

pub use pg_worker_core::writer_brief::{Input, Policy, Review};

const PREFIX: &str = "PLANGRAPH-BATCH-INPUT-V1\n";
const SKILL: &str = include_str!("../../../skills/batch-writer/SKILL.md");

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
    if let Some(input) = Input::decode(text)? {
        return render_items(&input, None).map(Cow::Owned);
    }
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

fn render_items(input: &Input, skill: Option<&str>) -> Result<String, String> {
    let mut out = "# Batch writer brief\n".to_owned();
    if skill.is_some() {
        writeln!(out, "{}", pg_worker_core::writer_brief::RENDERED_MARKER).unwrap();
    }
    out.push_str("\n# Items\n");
    for item in &input.items {
        writeln!(
            out,
            "\n### {} | {}\n{}\n\nDone criteria:\n{}",
            item.key,
            item.title,
            item.description.trim_end(),
            item.done_criteria.trim_end()
        )
        .unwrap();
        if !item.files.is_empty() {
            writeln!(out, "\nFiles:\n{}", item.files.join("\n")).unwrap();
        }
        if let Some(context) = &item.context {
            writeln!(out, "\n{}", context.trim_end()).unwrap();
        }
    }
    if let Some(context) = &input.context {
        writeln!(out, "\n{}", context.trim_end()).unwrap();
    }
    if let Some(skill) = skill {
        writeln!(out, "\n{}", skill.trim_end()).unwrap();
    }
    out.push('\n');
    out.push_str(&input.run.render()?);
    Ok(out)
}

/// Compose the complete typed prompt. The delivery path owns the review mode and policy.
pub fn compose(
    input: &Input,
    agent: &str,
    driver_owned: bool,
    effective: Option<&serde_json::Value>,
) -> Result<String, String> {
    if driver_owned != matches!(input.run.review, Review::Driver {}) {
        return Err("batch writer review owner differs from the launch path".into());
    }
    if let Some(effective) = effective {
        let policy: Policy = serde_json::from_value(effective.clone())
            .map_err(|e| format!("invalid effective batch writer policy: {e}"))?;
        if policy != input.policy {
            return Err("stale batch writer policy: differs from effective launch policy".into());
        }
    }
    let role = if driver_owned {
        "native_implement"
    } else {
        "implement"
    };
    let head = crate::role_header::header(agent, role);
    Ok(format!("{head}\n\n{}", render_items(input, Some(SKILL))?))
}

/// Place launcher-generated context before the capped run block of a composed prompt.
pub fn with_launch_context(prompt: &str, context: &str) -> String {
    if prompt
        .lines()
        .take(4)
        .any(|line| line == pg_worker_core::writer_brief::RENDERED_MARKER)
    {
        if let Some((body, run)) = prompt.rsplit_once("\n# Run values\n") {
            return format!("{body}\n\n{}\n\n# Run values\n{run}", context.trim_end());
        }
    }
    format!("{prompt}\n{}\n", context.trim_end())
}

#[cfg(test)]
mod tests {
    use super::{render, Cow, PREFIX};
    use serde_json::{json, Value};

    const FIXTURE: &str = include_str!("../tests/fixtures/batch-writer-input.txt");

    fn parse_fixture(text: &str) -> Value {
        let text = text.replace("\r\n", "\n");
        serde_json::from_str(text.strip_prefix(PREFIX).unwrap()).unwrap()
    }

    fn fixture() -> Value {
        parse_fixture(FIXTURE)
    }

    #[test]
    fn a_feeder_batch_renders_as_a_batch_for_the_batch_writer_skill() {
        // The golden brief the plangraph-cli feeder emits for a two-item batch.
        let text = include_str!("../tests/fixtures/feeder-batch-input.txt");
        let rendered = render(text).unwrap();
        assert!(crate::role_header::is_batch(&rendered), "{rendered}");
        assert_eq!(crate::role_header::skill_name(&rendered), "batch-writer");
        for part in [
            "### plangraph:ALPHA-1 | ALPHA-1",
            "### plangraph:ALPHA-2 | ALPHA-2",
            "# Run values",
            "Plan-Item: plangraph:ALPHA-2",
        ] {
            assert!(rendered.contains(part), "{part}");
        }
    }

    #[test]
    fn parses_fixture_with_either_line_ending() {
        let lf = FIXTURE.replace("\r\n", "\n");
        let crlf = lf.replace('\n', "\r\n");
        assert_eq!(parse_fixture(&crlf), parse_fixture(&lf));
    }

    fn assert_rejected(input: Value, error: &str) {
        let result = render(&format!("{PREFIX}{input}")).unwrap_err();
        assert!(result.starts_with(error), "{input}: {result}");
    }

    #[test]
    fn rejects_invalid_run_values() {
        for (field, value) in [
            ("round_cap", json!(0)),
            ("round_cap", json!(9)),
            ("review_wait_seconds", json!(0)),
            ("fast_checks", json!([])),
            ("trailers", json!([])),
            ("name", json!(" ")),
            ("worktree", json!("first\nsecond")),
            ("review_cmd", json!("review\tcommand")),
            ("fast_checks", json!(["check\r"])),
            ("trailers", json!(["Writer: fixture\u{0000}"])),
        ] {
            let mut input = fixture();
            input["run"][field] = value;
            assert_rejected(input, "invalid batch writer Run values");
        }
    }

    #[test]
    fn accepts_round_cap_boundaries() {
        for cap in [1, 8] {
            let mut input = fixture();
            input["run"]["round_cap"] = json!(cap);
            let text = format!("{PREFIX}{input}");
            assert!(render(&text)
                .unwrap()
                .contains(&format!("ROUND_CAP: {cap}\n")));
        }
    }

    #[test]
    fn rejects_missing_or_invalid_items() {
        let mut input = fixture();
        input["items"] = json!([]);
        assert_rejected(input, "batch writer input requires at least one item");

        for (field, value) in [("key", " "), ("title", "one\ntwo"), ("brief", " \n")] {
            let mut input = fixture();
            input["items"][0][field] = json!(value);
            assert_rejected(
                input,
                "batch writer input requires an item key, title and brief",
            );
        }
    }

    #[test]
    fn rejects_bad_json_and_unknown_fields() {
        let text = format!("{PREFIX}{{");
        assert!(render(&text)
            .unwrap_err()
            .starts_with("invalid batch writer input:"));

        for location in ["", "/items/0", "/run"] {
            let mut input = fixture();
            input.pointer_mut(location).unwrap()["extra"] = json!(true);
            assert_rejected(input, "invalid batch writer input: unknown field `extra`");
        }
    }

    #[test]
    fn renders_crlf_prefix_like_lf_prefix() {
        let lf = FIXTURE.replace("\r\n", "\n");
        let crlf = lf.replacen('\n', "\r\n", 1);
        let rendered = render(&crlf).unwrap();
        assert!(matches!(rendered, Cow::Owned(_)));
        assert_eq!(rendered, render(&lf).unwrap());
    }

    #[test]
    fn borrows_plain_text_unchanged() {
        let text = "Plain brief\r\nPLANGRAPH-BATCH-INPUT-V1\n  keep whitespace  \n";
        assert!(matches!(render(text).unwrap(), Cow::Borrowed(brief) if brief == text));
    }

    fn full_input() -> Value {
        json!({
            "items": [{
                "key": "demo:FULL", "title": "Complete item",
                "description": format!("Why: {}\nHow: Keep the last sentence.", "detail ".repeat(50)),
                "done_criteria": "Preserve the final criterion."
            }],
            "run": {
                "name": "fixture-batch", "base": "origin/main", "status_file": "status.txt",
                "review": {"owner": "driver"}, "policy_revision": "current",
                "rules": [{"id": "affected_tests", "jobs": 2}],
                "trailers": ["Plan-Item: demo:FULL", "Writer: fixture"]
            },
            "policy": {"revision": "current", "rules": [{"id": "affected_tests", "jobs": 2}]}
        })
    }

    #[test]
    fn batch_writer_rule_conflicts_are_rejected() {
        let mut input = full_input();
        let wire = |input: &Value| format!("{}\n{input}", pg_worker_core::writer_brief::PREFIX);
        let text = wire(&input);
        let prompt = render(&text).unwrap();
        let (spec, rules) = prompt.split_once("\n# Run values\n").unwrap();
        assert!(spec.contains(input["items"][0]["description"].as_str().unwrap()));
        assert!(spec.contains("Preserve the final criterion."));
        assert!(rules.contains("--files-only --run -n 2"));
        assert!(rules.split_whitespace().count() <= 150);
        for (path, value, reason) in [
            (
                "/run/review",
                json!({"owner":"driver", "command":"review"}),
                "invalid",
            ),
            ("/run/rules", json!([{"id":"reset"}]), "invalid"),
            ("/run/rules", json!([{"id":"full_suite"}]), "invalid"),
            ("/run/rules", json!([{"id":"keeper_lookup"}]), "invalid"),
            ("/run/policy_revision", json!("old"), "stale"),
            (
                "/items/0/description",
                json!("Why: truncated …[+20 chars]\nHow: do it"),
                "truncat",
            ),
            ("/items/0/done_criteria", json!("# Empty\n"), "heading"),
        ] {
            let old = std::mem::replace(input.pointer_mut(path).unwrap(), value);
            let error = render(&wire(&input)).expect_err(path);
            assert!(error.contains(reason), "{path}: {error}");
            *input.pointer_mut(path).unwrap() = old;
        }
    }
}
