//! The batch writer input shared by the fleet Claude launcher and native Codex.

use serde::Deserialize;
use std::{borrow::Cow, fmt::Write, path::Path};

const PREFIX: &str = "PLANGRAPH-BATCH-INPUT-V1\n";
const FAST_CHECKS_COMMAND: &str = "plangraph fast-checks --owned-worktree";

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
    /// Older producers still send their own list; the brief names the one stamp command instead.
    #[serde(default)]
    #[allow(dead_code)]
    fast_checks: Vec<String>,
    /// The writer pushes `lane/<NAME>` with its receipt once per round, on the stamped final
    /// head; a producer whose host pushes the accepted head itself (the feeder) sets this to
    /// `false`.
    #[serde(default = "push_by_default")]
    push: bool,
    review_cmd: String,
    review_wait_seconds: u64,
    round_cap: u8,
    trailers: Vec<String>,
}

fn push_by_default() -> bool {
    true
}

fn single_line(value: &str) -> bool {
    !value.trim().is_empty() && !value.chars().any(char::is_control)
}

impl RunValues {
    fn render(self, brief: &mut String, checkout: &Path) -> Result<(), String> {
        let worktree = self.worktree.clone();
        let name = self.name.clone();
        let fields = [
            ("NAME", self.name.clone()),
            ("WORKTREE", self.worktree),
            ("BASE", self.base),
            ("WRITER_FAMILY", self.writer_family),
            ("STATUS_FILE", self.status_file),
        ];
        if fields.iter().any(|(_, value)| !single_line(value))
            || !single_line(&self.review_cmd)
            || self.review_wait_seconds == 0
            || !(1..=8).contains(&self.round_cap)
            || self.trailers.is_empty()
            || self.trailers.iter().any(|value| !single_line(value))
        {
            return Err("invalid batch writer Run values".into());
        }
        brief.push_str("# Run values\n");
        for (label, value) in fields {
            writeln!(brief, "{label}: {value}").unwrap();
        }
        // One command runs every fast check of the head and writes its receipt; a list of
        // separate commands is how a failing early batch was once read past.
        writeln!(brief, "FAST_CHECKS: {FAST_CHECKS_COMMAND}").unwrap();
        if self.push {
            // The writer's own head, expanded by its shell: the receipt ref is keyed by the head sha.
            let argv = pg_worker_core::fast_check_stamp::push_argv(
                "$(git rev-parse HEAD)",
                &format!("lane/{name}"),
            );
            writeln!(brief, "PUSH: git {}", argv.join(" ")).unwrap();
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
        // The live list of the checkout, never a copy: an edit of one of these needs a router Gate-Edit D-id.
        // The real checkout of the caller is read first; `WORKTREE` may be a display value.
        let paths = match crate::brief_gate_paths::read(&checkout.to_string_lossy())? {
            Some(paths) => Some(paths),
            None => crate::brief_gate_paths::read(&worktree)?,
        };
        if let Some(paths) = paths {
            writeln!(
                brief,
                "GATE_PATHS (do not edit; a Gate-Edit D-id is required): {}",
                paths.join(" ")
            )
            .unwrap();
        }
        Ok(())
    }
}

/// The parsed versioned batch input of `text`; none for a plain brief.
fn parse(text: &str) -> Option<Result<BatchInput, String>> {
    let payload = text.strip_prefix(PREFIX).or_else(|| {
        text.strip_prefix(PREFIX.trim_end_matches('\n'))?
            .strip_prefix("\r\n")
    })?;
    Some(serde_json::from_str(payload).map_err(|err| format!("invalid batch writer input: {err}")))
}

/// The members of a batch input, checked once for the renderer and for admission: at least one,
/// each a one-line key, title and brief, and each key exactly an item key as run records keep
/// it, so the key the writer reads in its heading is the key that was admitted.
fn check_items(items: &[Item]) -> Result<(), String> {
    if items.is_empty() {
        return Err("batch writer input requires at least one item".into());
    }
    for item in items {
        if !single_line(&item.key) || !single_line(&item.title) || item.brief.trim().is_empty() {
            return Err("batch writer input requires an item key, title and brief".into());
        }
        if !crate::lane_guard::valid_key(&item.key) {
            return Err(format!(
                "batch writer input item key is not a valid item key: {:?}",
                item.key
            ));
        }
    }
    Ok(())
}

/// The item keys a versioned batch input assigns, in its own `items[].key` order: its typed
/// membership, which no heading inside an item body can extend. A malformed member refuses the
/// whole input. None for a plain brief.
pub fn typed_keys(text: &str) -> Option<Result<Vec<String>, String>> {
    Some(parse(text)?.and_then(|input| {
        check_items(&input.items)?;
        Ok(input.items.into_iter().map(|item| item.key).collect())
    }))
}

/// Render a versioned batch input for the checkout of the current directory.
pub fn render(text: &str) -> Result<Cow<'_, str>, String> {
    render_in(text, &std::env::current_dir().unwrap_or_default())
}

/// Render a versioned batch input in the prompt transport. Plain briefs pass through. `checkout`
/// is the writer's real checkout, where the live gate path list is read.
pub fn render_in<'a>(text: &'a str, checkout: &Path) -> Result<Cow<'a, str>, String> {
    let Some(parsed) = parse(text) else {
        return Ok(Cow::Borrowed(text));
    };
    let BatchInput { items, run } = parsed?;
    check_items(&items)?;
    let mut rendered = "# Batch writer brief\n\n# Items\n".to_owned();
    for item in items {
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
    run.render(&mut rendered, checkout)?;
    Ok(Cow::Owned(rendered))
}

#[cfg(test)]
mod tests {
    use super::{render, render_in, typed_keys, Cow, PREFIX};
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
            ("trailers", json!([])),
            ("name", json!(" ")),
            ("worktree", json!("first\nsecond")),
            ("review_cmd", json!("review\tcommand")),
            ("trailers", json!(["Writer: fixture\u{0000}"])),
        ] {
            let mut input = fixture();
            input["run"][field] = value;
            assert_rejected(input, "invalid batch writer Run values");
        }
    }

    #[test]
    fn a_brief_lists_every_gate_path_of_the_checked_out_tree() {
        let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
        let mut input = fixture();
        input["run"]["worktree"] = json!(root.to_str().unwrap());
        let text = format!("{PREFIX}{input}");
        let rendered = render(&text).unwrap();
        let line = rendered
            .lines()
            .find(|line| line.starts_with("GATE_PATHS "))
            .unwrap();
        let live = crate::brief_gate_paths::read(root.to_str().unwrap())
            .unwrap()
            .unwrap();
        assert!(live.len() >= 7, "{live:?}");
        for path in &live {
            assert!(line.contains(path.as_str()), "{path} missing from {line}");
        }
        // A worktree that is not a checkout has no list to print and the brief stays valid.
        assert!(!render(FIXTURE).unwrap().contains("GATE_PATHS"));
    }

    #[test]
    fn a_brief_names_the_one_stamp_command_and_pushes_the_lane_by_default() {
        let mut input = fixture();
        input["run"]["fast_checks"] = json!(["git diff --check", "cargo fmt --check"]);
        let text = format!("{PREFIX}{input}");
        let rendered = render(&text).unwrap();
        assert!(
            rendered.contains("\nFAST_CHECKS: plangraph fast-checks --owned-worktree\n"),
            "{rendered}"
        );
        assert!(!rendered.contains("cargo fmt --check"), "{rendered}");
        let name = input["run"]["name"].as_str().unwrap();
        // The head and its receipt ref in one atomic push, the form the lane hook requires.
        assert!(
            rendered.contains(&format!(
                "\nPUSH: git push --atomic origin $(git rev-parse HEAD):refs/heads/lane/{name} +refs/plangraph/receipts/$(git rev-parse HEAD):{}\n",
                pg_worker_core::fast_check_stamp::receipt_ref_for(&format!("lane/{name}"))
            )),
            "{rendered}"
        );
        // A producer whose host pushes the accepted head itself opts out.
        input["run"]["push"] = json!(false);
        let text = format!("{PREFIX}{input}");
        assert!(!render(&text).unwrap().contains("\nPUSH:"));
    }

    #[test]
    fn the_feeder_brief_lists_the_gate_paths_of_the_real_checkout_not_its_display_worktree() {
        // The feeder's WORKTREE is a sentence, not a path: the caller's checkout supplies the list.
        let text = include_str!("../tests/fixtures/feeder-batch-input.txt");
        let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
        let rendered = render_in(text, &root).unwrap();
        let line = rendered
            .lines()
            .find(|line| line.starts_with("GATE_PATHS "))
            .unwrap_or_else(|| panic!("no GATE_PATHS line in {rendered}"));
        let live = crate::brief_gate_paths::read(root.to_str().unwrap())
            .unwrap()
            .unwrap();
        for path in &live {
            assert!(line.contains(path.as_str()), "{path} missing from {line}");
        }
        assert!(
            !rendered.contains("\nPUSH:"),
            "the feeder's host pushes the head itself"
        );
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
    fn rejects_an_item_key_the_writer_would_read_as_another_key() {
        for key in [
            "demo:HELD ",
            " demo:HELD",
            "demo:HELD|x",
            "nokey",
            "demo:",
            "a b:c",
        ] {
            let mut input = fixture();
            input["items"][0]["key"] = json!(key);
            let text = format!("{PREFIX}{input}");
            let error = render(&text).unwrap_err();
            assert!(error.contains("not a valid item key"), "{key:?}: {error}");
            assert_eq!(typed_keys(&text), Some(Err(error)));
        }
        assert_eq!(typed_keys("plain brief"), None);
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
}
