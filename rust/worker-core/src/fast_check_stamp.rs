//! The fast-checks receipt of one head: its sha, one verdict and one line per check.
//!
//! A reader asks `green`; a missing, red, malformed or other-head stamp is not green. Receipts
//! written before this format (`green` or the bare sha) stay green.

use crate::git::{git, git_stdin};
use crate::worktree::fast_check_stamp;
use std::path::{Path, PathBuf};

/// Prefix of every published receipt ref: the local one and the per-lane refs a push creates.
const RECEIPT_REF_PREFIX: &str = "refs/notes/plangraph-fast-checks";

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

/// Whether `ref_name` is a published receipt ref: a per-lane `refs/notes/plangraph-fast-checks-<lane>`.
pub fn is_receipt_ref(ref_name: &str) -> bool {
    ref_name
        .strip_prefix(RECEIPT_REF_PREFIX)
        .is_some_and(|rest| rest.starts_with('-') && rest.len() > 1)
}

/// The receipt ref a lane publishes: one ref per lane, so two writers never share a history.
/// Injective: every byte outside `[A-Za-z0-9._]` is `%XX`, so `lane/a-b` and `lane/a/b` differ.
pub fn receipt_ref_for(target: &str) -> String {
    let mut lane = String::new();
    for byte in target.trim_start_matches("refs/heads/").bytes() {
        if byte.is_ascii_alphanumeric() || byte == b'.' || byte == b'_' {
            lane.push(char::from(byte));
        } else {
            lane.push_str(&format!("%{byte:02x}"));
        }
    }
    format!("{RECEIPT_REF_PREFIX}-{lane}")
}

/// The local ref that holds the receipt commit of one head (distinct per head, so concurrent
/// producers never write the same ref).
pub fn local_receipt_ref(head: &str) -> String {
    format!("refs/plangraph/receipts/{head}")
}

/// The one push of a lane head and its receipt: atomic, so a lane update never lands without
/// the receipt (the pre-push hook refuses a lane head whose designated receipt is not in the
/// same push).
pub fn push_argv(head: &str, target: &str) -> Vec<String> {
    let target = if target.starts_with("refs/") {
        target.to_owned()
    } else {
        format!("refs/heads/{target}")
    };
    vec![
        "push".into(),
        "--atomic".into(),
        "origin".into(),
        format!("{head}:{target}"),
        // Forced: the per-lane receipt ref has one writer and holds one parentless commit.
        format!("+{}:{}", local_receipt_ref(head), receipt_ref_for(&target)),
    ]
}

/// Write the receipt of `head` once, for every producer: the shared state file (atomically) and
/// a parentless commit whose tree holds only that receipt (what a push publishes, so nothing but
/// this receipt is ever reachable from the pushed ref). Returns the state file.
pub fn publish(repo: &Path, head: &str, checks: &[Check]) -> Result<PathBuf, String> {
    let stamp = fast_check_stamp(repo, head).map_err(|e| e.to_string())?;
    let state = stamp.parent().ok_or("stamp directory missing")?;
    std::fs::create_dir_all(state).map_err(|e| e.to_string())?;
    let text = render(head, checks);
    let mut file = tempfile::NamedTempFile::new_in(state).map_err(|e| e.to_string())?;
    std::io::Write::write_all(&mut file, text.as_bytes()).map_err(|e| e.to_string())?;
    file.persist(&stamp).map_err(|e| e.to_string())?;
    // A head that is not a commit of this repository has nothing to publish; the push of its
    // receipt ref then fails loudly instead of the receipt silently missing.
    if git(repo, &["cat-file", "-e", &format!("{head}^{{commit}}")]).is_err() {
        return Ok(stamp);
    }
    let blob =
        git_stdin(repo, &["hash-object", "-w", "--stdin"], &text).map_err(|e| e.to_string())?;
    let tree = git_stdin(repo, &["mktree"], &format!("100644 blob {blob}\t{head}\n"))
        .map_err(|e| e.to_string())?;
    let commit = git(
        repo,
        &[
            "-c",
            "user.name=plangraph",
            "-c",
            "user.email=plangraph@localhost",
            "commit-tree",
            &tree,
            "-m",
            "fast-checks receipt",
        ],
    )
    .map_err(|e| e.to_string())?;
    git(repo, &["update-ref", &local_receipt_ref(head), &commit])
        .map_err(|e| format!("receipt ref not written: {e}"))?;
    Ok(stamp)
}

/// A receipt text that is well formed for the note named `name`, whatever its verdict.
fn well_formed(text: &str, name: &str) -> bool {
    let (mut heads, mut verdicts, mut checks) = (0, 0, 0);
    for line in text.lines() {
        if let Some(found) = line.strip_prefix("head ") {
            if found.trim() != name {
                return false;
            }
            heads += 1;
        } else if let Some(found) = line.strip_prefix("result ") {
            if !matches!(found.trim(), "green" | "red") {
                return false;
            }
            verdicts += 1;
        } else if let Some(rest) = line.strip_prefix("check ") {
            let mut words = rest.split_whitespace();
            let ok = matches!(
                (words.next(), words.next(), words.next(), words.next()),
                (Some(label), Some(rc), Some(wall), None)
                    if label.bytes().all(|b| b.is_ascii_alphanumeric() || b"/._-".contains(&b))
                        && rc.strip_prefix("rc=").is_some_and(|n| n.parse::<i32>().is_ok())
                        && wall.strip_prefix("wall=").is_some_and(|n| n.parse::<f64>().is_ok())
            );
            if !ok {
                return false;
            }
            checks += 1;
        } else {
            return false;
        }
    }
    heads == 1 && verdicts == 1 && checks > 0
}

/// What a push may publish as a receipt ref: a notes tree of nothing but well-formed receipts
/// (each file named by the head it is for); with `head`, one of them a green receipt of it.
pub fn notes_commit_green(repo: &Path, commit: &str, head: Option<&str>) -> Result<(), String> {
    crate::git::check_rev(commit).map_err(|e| e.to_string())?;
    // Parentless: pushing the ref sends this one commit, so what is validated is all that is published.
    let line = git(repo, &["rev-list", "--parents", "--max-count=1", commit])
        .map_err(|e| e.to_string())?;
    if line.split_whitespace().count() != 1 {
        return Err(format!(
            "{commit} has parents: a receipt ref holds one parentless commit"
        ));
    }
    let listing =
        git(repo, &["ls-tree", "-r", "-z", "--full-tree", commit]).map_err(|e| e.to_string())?;
    let mut found = false;
    for entry in listing.split('\0').filter(|e| !e.is_empty()) {
        let (meta, path) = entry.split_once('\t').ok_or("malformed tree entry")?;
        let mut fields = meta.split_whitespace();
        let (_mode, kind, sha) = (fields.next(), fields.next(), fields.next());
        let name = path.replace('/', "");
        if kind != Some("blob")
            || !matches!(name.len(), 40 | 64)
            || !name.bytes().all(|b| b.is_ascii_hexdigit())
        {
            return Err(format!("not a receipt note: {path}"));
        }
        let text = git(
            repo,
            &["cat-file", "blob", sha.ok_or("malformed tree entry")?],
        )
        .map_err(|e| e.to_string())?;
        if !well_formed(&text, &name) {
            return Err(format!("malformed receipt for {name}"));
        }
        if let Some(head) = head.filter(|head| *head == name) {
            if !text_is_green(&text, head) {
                return Err(format!("receipt of {head} is not green"));
            }
            found = true;
        }
    }
    match head {
        Some(head) if !found => Err(format!("no receipt of {head} in {commit}")),
        _ => Ok(()),
    }
}

/// Whether the shared receipt of `head` exists and is green; a clone that fetched a receipt
/// ref with the pushed head has no stamp file and is judged by the notes it carries.
pub fn green(repo: &Path, head: &str) -> bool {
    let file = fast_check_stamp(repo, head)
        .ok()
        .and_then(|path| std::fs::read_to_string(path).ok());
    if let Some(text) = file {
        return text_is_green(&text, head);
    }
    // The local receipt commit of this head, or a fetched per-lane receipt ref (flat tree: path = head).
    let refs = git(
        repo,
        &["for-each-ref", "--format=%(refname)", "refs/notes/"],
    )
    .unwrap_or_default();
    std::iter::once(local_receipt_ref(head))
        .chain(
            refs.lines()
                .filter(|r| is_receipt_ref(r))
                .map(str::to_owned),
        )
        .any(|r| {
            git(repo, &["show", &format!("{r}:{head}")])
                .is_ok_and(|text| text_is_green(&text, head))
        })
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
    fn a_fetched_receipt_ref_stands_in_for_a_missing_stamp_file() {
        let (dir, head) = repo();
        publish(dir.path(), &head, &[check("fmt", 1)]).unwrap();
        std::fs::remove_file(fast_check_stamp(dir.path(), &head).unwrap()).unwrap();
        assert!(!green(dir.path(), &head), "a red receipt");
        publish(dir.path(), &head, &[check("fmt", 0)]).unwrap();
        std::fs::remove_file(fast_check_stamp(dir.path(), &head).unwrap()).unwrap();
        assert!(green(dir.path(), &head), "the local receipt commit");
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

    fn tip(dir: &Path, head: &str) -> String {
        git(dir, &["rev-parse", &local_receipt_ref(head)]).unwrap()
    }

    #[test]
    fn the_lane_push_is_atomic_and_carries_one_receipt_ref_per_lane() {
        let head = "a".repeat(40);
        let argv = push_argv(&head, "lane/pr10");
        assert_eq!(argv[..3], ["push", "--atomic", "origin"]);
        assert_eq!(argv[3], format!("{head}:refs/heads/lane/pr10"));
        assert_eq!(
            argv[4],
            format!("+refs/plangraph/receipts/{head}:refs/notes/plangraph-fast-checks-lane%2fpr10")
        );
        assert!(is_receipt_ref("refs/notes/plangraph-fast-checks-lane%2fx"));
        assert!(!is_receipt_ref("refs/notes/plangraph-fast-checks"));
        assert!(!is_receipt_ref("refs/notes/plangraph-fast-checks-"));
        assert!(!is_receipt_ref("refs/notes/commits"));
        assert!(!is_receipt_ref("refs/heads/plangraph-fast-checks-x"));
    }

    #[test]
    fn two_lanes_never_share_a_receipt_ref() {
        // Both spellings used to map to one force-updated ref.
        assert_ne!(receipt_ref_for("lane/a-b"), receipt_ref_for("lane/a/b"));
        assert_ne!(receipt_ref_for("lane/a%2fb"), receipt_ref_for("lane/a/b"));
        assert_eq!(
            receipt_ref_for("refs/heads/lane/x"),
            receipt_ref_for("lane/x")
        );
    }

    #[test]
    fn a_published_receipt_is_a_state_file_and_one_parentless_commit_that_validates() {
        let (dir, head) = repo();
        publish(dir.path(), &head, &[check("fmt", 0), check("tests", 0)]).unwrap();
        assert!(green(dir.path(), &head));
        let tip = tip(dir.path(), &head);
        assert_eq!(notes_commit_green(dir.path(), &tip, Some(&head)), Ok(()));
        assert_eq!(notes_commit_green(dir.path(), &tip, None), Ok(()));
        // An ordinary commit is not a receipt ref, and another head has no receipt in it.
        assert!(notes_commit_green(dir.path(), &head, Some(&head)).is_err());
        assert!(notes_commit_green(dir.path(), &tip, Some(&"b".repeat(40))).is_err());
    }

    #[test]
    fn a_receipt_commit_with_a_parent_is_not_publishable_so_history_is_never_sent() {
        let (dir, head) = repo();
        publish(dir.path(), &head, &[check("fmt", 0)]).unwrap();
        let first = tip(dir.path(), &head);
        // A second commit over the first: content only in its parent would be published unchecked.
        let tree = git(dir.path(), &["rev-parse", &format!("{first}^{{tree}}")]).unwrap();
        let child = git(
            dir.path(),
            &[
                "-c",
                "user.name=t",
                "-c",
                "user.email=t@example.com",
                "commit-tree",
                &tree,
                "-p",
                &first,
                "-m",
                "child",
            ],
        )
        .unwrap();
        let err = notes_commit_green(dir.path(), &child, Some(&head)).unwrap_err();
        assert!(err.contains("has parents"), "{err}");
    }

    #[test]
    fn a_red_receipt_is_not_publishable_and_a_tampered_tree_is_malformed() {
        let (dir, head) = repo();
        publish(dir.path(), &head, &[check("fmt", 1)]).unwrap();
        let red = tip(dir.path(), &head);
        assert!(notes_commit_green(dir.path(), &red, Some(&head))
            .unwrap_err()
            .contains("not green"));
        // A tree whose file is not a receipt at all.
        let blob = git_stdin(
            dir.path(),
            &["hash-object", "-w", "--stdin"],
            "this note is not a receipt at all",
        )
        .unwrap();
        let tree = git_stdin(
            dir.path(),
            &["mktree"],
            &format!("100644 blob {blob}\t{head}\n"),
        )
        .unwrap();
        let bad = git(
            dir.path(),
            &[
                "-c",
                "user.name=t",
                "-c",
                "user.email=t@example.com",
                "commit-tree",
                &tree,
                "-m",
                "x",
            ],
        )
        .unwrap();
        assert!(notes_commit_green(dir.path(), &bad, Some(&head))
            .unwrap_err()
            .contains("malformed"));
    }

    #[test]
    fn a_fetched_lane_receipt_ref_is_read_like_the_local_one() {
        let (dir, head) = repo();
        publish(dir.path(), &head, &[check("fmt", 0)]).unwrap();
        let lane = receipt_ref_for("lane/x");
        git(dir.path(), &["update-ref", &lane, &tip(dir.path(), &head)]).unwrap();
        git(dir.path(), &["update-ref", "-d", &local_receipt_ref(&head)]).unwrap();
        std::fs::remove_file(fast_check_stamp(dir.path(), &head).unwrap()).unwrap();
        assert!(green(dir.path(), &head));
    }

    #[test]
    fn concurrent_producers_write_distinct_receipts_without_a_shared_ref() {
        let (dir, first) = repo();
        crate::testutil::write(dir.path(), "second.txt", "2\n");
        let second = crate::testutil::commit_all(dir.path(), "second");
        let root = dir.path().to_path_buf();
        let workers: Vec<_> = [first.clone(), second.clone()]
            .into_iter()
            .map(|head| {
                let root = root.clone();
                std::thread::spawn(move || publish(&root, &head, &[check("fmt", 0)]).unwrap())
            })
            .collect();
        for worker in workers {
            worker.join().unwrap();
        }
        assert!(green(dir.path(), &first) && green(dir.path(), &second));
        assert_ne!(tip(dir.path(), &first), tip(dir.path(), &second));
    }
}
