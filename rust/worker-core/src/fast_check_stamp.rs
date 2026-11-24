//! The fast-checks receipt of one head: its sha, one verdict and one line per check.
//!
//! A reader asks `green`; a missing, red, malformed or other-head stamp is not green. Receipts
//! written before this format (`green` or the bare sha) stay green.

use crate::git::git;
use crate::worktree::fast_check_stamp;
use std::path::Path;

/// Git notes ref that carries the receipt with a pushed head.
pub const NOTES_REF: &str = "plangraph-fast-checks";

/// One executed check: its label, exit code and wall seconds.
#[derive(Debug, Clone, PartialEq)]
pub struct Check {
    pub label: String,
    pub rc: i32,
    pub seconds: f64,
}

impl Check {
    pub fn line(&self) -> String {
        format!(
            "check {} rc={} wall={:.3}",
            self.label, self.rc, self.seconds
        )
    }
}

/// The receipt text: red when any check failed or none ran.
pub fn render(head: &str, checks: &[Check]) -> String {
    let verdict = if !checks.is_empty() && checks.iter().all(|c| c.rc == 0) {
        "green"
    } else {
        "red"
    };
    let mut text = format!("head {head}\nresult {verdict}\n");
    for check in checks {
        text.push_str(&check.line());
        text.push('\n');
    }
    text
}

/// Whether `text` is a green receipt for exactly `head`.
///
/// A structured receipt needs its own `head` line, exactly one `result green` and at least one
/// well-formed `check <label> rc=0 wall=<s>` line; a truncated file is never green. Only the
/// old one-word receipts (`green` or the bare sha) stay accepted without check lines.
pub fn text_is_green(text: &str, head: &str) -> bool {
    let (mut heads, mut verdicts, mut checks) = (Vec::new(), Vec::new(), 0usize);
    for line in text.lines() {
        if let Some(found) = line.strip_prefix("head ") {
            heads.push(found.trim());
        } else if let Some(found) = line.strip_prefix("result ") {
            verdicts.push(found.trim());
        } else if line.starts_with("check ") {
            if !valid_check_line(line) {
                return false;
            }
            checks += 1;
        }
    }
    if heads.is_empty() && verdicts.is_empty() && checks == 0 {
        return matches!(text.trim(), "green") || (!head.is_empty() && text.trim() == head);
    }
    heads == [head] && verdicts == ["green"] && checks > 0
}

/// `check <label> rc=0 wall=<seconds>`: a green check line with every field present.
fn valid_check_line(line: &str) -> bool {
    let mut words = line.split_whitespace();
    let (Some("check"), Some(label), Some(rc), Some(wall), None) = (
        words.next(),
        words.next(),
        words.next(),
        words.next(),
        words.next(),
    ) else {
        return false;
    };
    !label.is_empty()
        && rc == "rc=0"
        && wall
            .strip_prefix("wall=")
            .is_some_and(|s| s.parse::<f64>().is_ok())
}

/// Whether the shared receipt of `head` exists and is green; a clone that fetched the receipt
/// note with the pushed head has no stamp file and is judged by the note.
pub fn green(repo: &Path, head: &str) -> bool {
    let file = fast_check_stamp(repo, head)
        .ok()
        .and_then(|path| std::fs::read_to_string(path).ok());
    match file {
        Some(text) => text_is_green(&text, head),
        None => git(
            repo,
            &["notes", &format!("--ref={NOTES_REF}"), "show", head],
        )
        .is_ok_and(|text| text_is_green(&text, head)),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testutil::repo;

    fn check(label: &str, rc: i32) -> Check {
        Check {
            label: label.into(),
            rc,
            seconds: 1.5,
        }
    }

    #[test]
    fn a_receipt_names_its_head_and_every_check() {
        let text = render(
            &"a".repeat(40),
            &[check("affected-tests", 0), check("fmt", 0)],
        );
        assert!(text.starts_with(&format!("head {}\nresult green\n", "a".repeat(40))));
        assert!(text.contains("check affected-tests rc=0 wall=1.500\n"));
        assert!(text_is_green(&text, &"a".repeat(40)));
    }

    #[test]
    fn a_failed_check_or_another_head_is_not_green() {
        let head = "b".repeat(40);
        let red = render(&head, &[check("fmt", 0), check("affected-tests", 1)]);
        assert!(red.contains("result red"));
        assert!(!text_is_green(&red, &head));
        assert!(!text_is_green(
            &render(&head, &[check("fmt", 0)]),
            &"c".repeat(40)
        ));
    }

    #[test]
    fn a_hand_edited_green_over_a_red_check_is_not_green() {
        let head = "d".repeat(40);
        let text = format!("head {head}\nresult green\ncheck affected-tests rc=1 wall=2.000\n");
        assert!(!text_is_green(&text, &head));
    }

    #[test]
    fn an_empty_or_check_less_receipt_is_not_green() {
        let head = "f".repeat(40);
        assert!(!text_is_green("", &head));
        assert!(!text_is_green("\n", &head));
        assert!(!text_is_green(
            &format!("head {head}\nresult green\n"),
            &head
        ));
        assert!(!text_is_green(
            "result green\ncheck fmt rc=0 wall=1.0\n",
            &head
        ));
        let twice = format!("head {head}\nresult green\nresult green\ncheck fmt rc=0 wall=1.0\n");
        assert!(!text_is_green(&twice, &head));
        let garbled = format!("head {head}\nresult green\ncheck fmt rc=0\n");
        assert!(!text_is_green(&garbled, &head));
        let ok = format!("head {head}\nresult green\ncheck fmt rc=0 wall=1.0\n");
        assert!(text_is_green(&ok, &head));
    }

    #[test]
    fn receipts_of_the_old_format_stay_green() {
        let head = "e".repeat(40);
        assert!(text_is_green("green\n", &head));
        assert!(text_is_green(&format!("{head}\n"), &head));
        assert!(!text_is_green("red\n", &head));
        assert!(!text_is_green("something else\n", &head));
    }

    #[test]
    fn a_fetched_note_stands_in_for_a_missing_stamp_file() {
        let (dir, head) = repo();
        let notes = format!("--ref={NOTES_REF}");
        let note = |text: &str| {
            git(
                dir.path(),
                &[
                    "-c",
                    "user.name=t",
                    "-c",
                    "user.email=t@example.com",
                    "notes",
                    &notes,
                    "add",
                    "-f",
                    "-m",
                    text,
                    &head,
                ],
            )
            .unwrap();
        };
        note(&render(&head, &[check("fmt", 1)]));
        assert!(!green(dir.path(), &head), "a red note");
        note(&render(&head, &[check("fmt", 0)]));
        assert!(green(dir.path(), &head));
    }

    #[test]
    fn the_shared_receipt_is_read_by_head() {
        let (dir, head) = repo();
        assert!(!green(dir.path(), &head), "no receipt yet");
        let stamp = fast_check_stamp(dir.path(), &head).unwrap();
        std::fs::create_dir_all(stamp.parent().unwrap()).unwrap();
        std::fs::write(&stamp, render(&head, &[check("fmt", 2)])).unwrap();
        assert!(!green(dir.path(), &head), "a red receipt");
        std::fs::write(&stamp, render(&head, &[check("fmt", 0)])).unwrap();
        assert!(green(dir.path(), &head));
    }
}
