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

/// The receipt text: red when any check failed (or none ran for a red verdict).
pub fn render(head: &str, checks: &[Check]) -> String {
    let verdict = if checks.iter().all(|c| c.rc == 0) {
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
pub fn text_is_green(text: &str, head: &str) -> bool {
    let mut verdict = None;
    for line in text.lines() {
        if let Some(found) = line.strip_prefix("head ") {
            if found.trim() != head {
                return false;
            }
        } else if let Some(found) = line.strip_prefix("result ") {
            verdict = Some(found.trim());
        } else if line.starts_with("check ") && !line.contains(" rc=0 ") {
            return false;
        }
    }
    match verdict {
        Some(verdict) => verdict == "green",
        None => matches!(text.trim(), "green" | "") || text.trim() == head,
    }
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
