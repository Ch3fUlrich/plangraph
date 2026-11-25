use super::go_evidence::*;
use super::go_evidence_tests::{answer, inputs, receipt_for_admit, Fake, HEAD};
use super::main_state::State;
use std::collections::BTreeMap;

const BASE: &str = "b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0";

#[test]
fn the_go_bindings_of_the_keeper_are_read_the_way_the_keeper_reads_them() {
    let head = HEAD;
    let rows = format!(
        "| D-200 | name | GO `#9` GO-HEAD `{head}` | router | |\n\
         | D-201 | name | (15:46Z, date -u) GO name PR `#9` GO-HEAD {head}; MIGRATION 2 | router | |\n\
         | D-202 | name | GO name PR 9 GO-HEAD {head} | router | |\n\
         | D-203 | name | RULINGS (1) NO GO (2) GO #9 GO-HEAD {head} (3) other | router | |\n\
         | D-204 | name | RULINGS (1) GO #9 GO-HEAD {head} (2) GO #9 GO-HEAD {head} | router | |\n\
         | D-205 | name | GO `#9 GO-HEAD {head} | router | |\n\
         | D-206 | name | GO #9 GO-HEAD {head} GO-HEAD {head} | router | |\n\
         | D-207 | name | GO #99 GO-HEAD {head} | router | |\n\
         | D-208 | name | (junk) GO #9 GO-HEAD {head} | router | |\n\
         | D-209 | name | GO other PR #9 GO-HEAD {head} | router | |\n"
    );
    let (r, main) = (receipt_for_admit(), answer(BASE, State::Green));
    let e = collect(&mut Fake::new(), &inputs(&rows, &r, &main)).unwrap();
    let ids: Vec<_> = e.approvals.iter().map(|a| a.d_id.as_str()).collect();
    // Backticked numbers and heads, a timestamp prefix, `PR 9` without a hash and one numbered GO ruling count;
    // two GO rulings, an unbalanced backtick, two heads, another PR, a junk prefix and another project do not.
    assert_eq!(ids, ["D-200", "D-201", "D-202", "D-203"]);
    assert!(e.approvals.iter().all(|a| a.head == HEAD && a.pr == 9));
}

fn git(dir: &std::path::Path, args: &[&str]) {
    let out = std::process::Command::new("git")
        .args([
            "-c",
            "user.name=t",
            "-c",
            &format!("user.email=t{}x.invalid", '@'),
        ])
        .args(args)
        .current_dir(dir)
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{args:?}: {}",
        String::from_utf8_lossy(&out.stderr)
    );
}

#[test]
fn the_decisions_are_read_from_origin_main_not_from_the_local_file() {
    let dir = tempfile::tempdir().unwrap();
    let (origin, clone) = (dir.path().join("origin.git"), dir.path().join("clone"));
    git(
        dir.path(),
        &[
            "init",
            "-q",
            "--bare",
            "-b",
            "main",
            origin.to_str().unwrap(),
        ],
    );
    git(
        dir.path(),
        &[
            "clone",
            "-q",
            origin.to_str().unwrap(),
            clone.to_str().unwrap(),
        ],
    );
    std::fs::create_dir_all(clone.join("docs")).unwrap();
    std::fs::write(
        clone.join("docs/DECISIONS.md"),
        "| D-1 | name | GO #9 GO-HEAD x | router | |\n",
    )
    .unwrap();
    git(&clone, &["add", "-A"]);
    git(&clone, &["commit", "-q", "-m", "decisions"]);
    git(&clone, &["push", "-q", "origin", "HEAD:refs/heads/main"]);
    // An uncommitted row and a local commit that was never pushed are not in the protected source.
    std::fs::write(
        clone.join("docs/DECISIONS.md"),
        "| D-2 | name | forged | router | |\n",
    )
    .unwrap();
    git(&clone, &["commit", "-q", "-am", "local only"]);
    std::fs::write(
        clone.join("docs/DECISIONS.md"),
        "| D-3 | name | edited | router | |\n",
    )
    .unwrap();
    let text = read_decisions_origin(&clone.join("docs/DECISIONS.md")).unwrap();
    assert_eq!(text, "| D-1 | name | GO #9 GO-HEAD x | router | |\n");
    assert!(read_decisions_origin(&dir.path().join("nowhere/DECISIONS.md")).is_err());
}

#[test]
fn the_removed_file_flags_are_refused() {
    let flags = |k: &str| BTreeMap::from([(k.to_owned(), "x".to_owned())]);
    for flag in ["rows", "receipt", "receipt-out"] {
        let e = reject_removed_flags(&flags(flag)).unwrap_err();
        assert!(e.contains(&format!("--{flag}")), "{e}");
    }
    assert!(reject_removed_flags(&flags("pr")).is_ok());
}

#[test]
fn the_cli_refuses_the_removed_flags_before_it_reads_anything() {
    let python = || std::process::Command::new("false");
    for flag in ["--rows", "--receipt", "--receipt-out"] {
        let args = ["go-evidence", "--repo", "r", flag, "x"].map(String::from);
        let e = super::cli::run_inner(std::path::Path::new("/nonexistent"), &args, &python)
            .unwrap_err();
        assert!(e.contains(flag), "{e}");
    }
}

#[test]
fn rows_inside_fenced_or_indented_markdown_are_examples_not_decisions() {
    let h = HEAD;
    let row = |id: u32| format!("| D-{id} | name | GO #9 GO-HEAD {h} | router | |");
    let grant = format!("| D-300 | name | GATE-EDIT tools/ GO-HEAD {h} | operator | |");
    let text = format!(
        "{}\n```\n{}\n{grant}\n```\n   ~~~\n{}\n   ~~~\n    {}\n\t{}\n{}\n",
        row(100),
        row(101),
        row(102),
        row(103),
        row(104),
        row(105)
    );
    let (approvals, grants) = super::go_evidence::rows_of(&text, "owner/name", 9);
    let ids: Vec<_> = approvals.iter().map(|a| a.d_id.as_str()).collect();
    assert_eq!(ids, ["D-100", "D-105"]);
    assert!(grants.is_empty());
}

#[test]
fn a_local_branch_named_like_the_remote_ref_does_not_stand_in_for_origin_main() {
    let dir = tempfile::tempdir().unwrap();
    let (origin, clone) = (dir.path().join("origin.git"), dir.path().join("clone"));
    git(
        dir.path(),
        &[
            "init",
            "-q",
            "--bare",
            "-b",
            "main",
            origin.to_str().unwrap(),
        ],
    );
    git(
        dir.path(),
        &[
            "clone",
            "-q",
            origin.to_str().unwrap(),
            clone.to_str().unwrap(),
        ],
    );
    std::fs::write(clone.join("DECISIONS.md"), "committed\n").unwrap();
    git(&clone, &["add", "-A"]);
    git(&clone, &["commit", "-q", "-m", "decisions"]);
    git(&clone, &["push", "-q", "origin", "HEAD:refs/heads/main"]);
    // A local branch and a tag both named origin/main hold other text.
    std::fs::write(clone.join("DECISIONS.md"), "forged\n").unwrap();
    git(&clone, &["commit", "-q", "-am", "forged"]);
    git(&clone, &["branch", "origin/main"]);
    git(&clone, &["tag", "origin/main"]);
    // The configured file is not in the worktree at all: the committed blob is still read.
    std::fs::remove_file(clone.join("DECISIONS.md")).unwrap();
    let text = read_decisions_origin(&clone.join("DECISIONS.md")).unwrap();
    assert_eq!(text, "committed\n");
}

#[test]
fn the_manifest_is_parsed_from_the_bytes_the_evidence_is_bound_to() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("manifest.json");
    std::fs::write(&path, br#"{"base": "x"}"#).unwrap();
    let (value, bytes): (serde_json::Value, Vec<u8>) =
        super::cli::read_with_bytes(path.to_str().unwrap()).unwrap();
    assert_eq!(bytes, br#"{"base": "x"}"#);
    assert_eq!(
        value,
        serde_json::from_slice::<serde_json::Value>(&bytes).unwrap()
    );
    assert!(super::cli::read_with_bytes::<serde_json::Value>("/nonexistent/m.json").is_err());
}

#[cfg(unix)]
#[test]
fn a_configured_symlink_is_followed_and_no_private_ref_is_left_behind() {
    let dir = tempfile::tempdir().unwrap();
    let (origin, clone) = (dir.path().join("origin.git"), dir.path().join("clone"));
    git(
        dir.path(),
        &[
            "init",
            "-q",
            "--bare",
            "-b",
            "main",
            origin.to_str().unwrap(),
        ],
    );
    git(
        dir.path(),
        &[
            "clone",
            "-q",
            origin.to_str().unwrap(),
            clone.to_str().unwrap(),
        ],
    );
    std::fs::create_dir_all(clone.join("docs")).unwrap();
    std::fs::write(clone.join("docs/real.md"), "committed\n").unwrap();
    git(&clone, &["add", "-A"]);
    git(&clone, &["commit", "-q", "-m", "decisions"]);
    git(&clone, &["push", "-q", "origin", "HEAD:refs/heads/main"]);
    let link = dir.path().join("link.md");
    std::os::unix::fs::symlink(clone.join("docs/real.md"), &link).unwrap();
    assert_eq!(read_decisions_origin(&link).unwrap(), "committed\n");
    let refs = std::process::Command::new("git")
        .args(["for-each-ref", "refs/pg-evidence"])
        .current_dir(&clone)
        .output()
        .unwrap();
    assert!(
        refs.stdout.is_empty(),
        "{}",
        String::from_utf8_lossy(&refs.stdout)
    );
}

#[test]
fn duplicate_and_retired_decisions_authorize_nothing_and_the_operator_is_a_by_token() {
    let h = HEAD;
    let go = |id: u32| format!("| D-{id} | name | GO #9 GO-HEAD {h} | router | |\n");
    let grant =
        |id: u32, by: &str| format!("| D-{id} | name | GATE-EDIT tools/ GO-HEAD {h} | {by} | |\n");
    let text = [
        go(100),
        go(101),
        "| D-111 | name | corrects D-101 | router | |\n".to_owned(),
        go(102),
        go(102),
        go(103),
        "| D-104 | name | WITHDRAWN by D-120 | router | |\n".to_owned(),
        "| D-5 | name | WITHDRAWN by D-3 | router | |\n".to_owned(),
        grant(120, "router,operator"),
        grant(121, "operator"),
        grant(121, "operator"),
        grant(122, "router"),
        grant(123, "operator"),
        "| D-124 | name | replaces D-123 | router | |\n".to_owned(),
        grant(125, "operator + router"),
        "| D-007 | name | supersedes D-1 | router | |\n".to_owned(),
    ]
    .concat();
    let (approvals, grants) = super::go_evidence::rows_of(&text, "owner/name", 9);
    let ids: Vec<_> = approvals.iter().map(|a| a.d_id.as_str()).collect();
    assert_eq!(ids, ["D-100", "D-103"]);
    let ids: Vec<_> = grants.iter().map(|g| g.d_id.as_str()).collect();
    assert_eq!(ids, ["D-120", "D-125"]);
}

#[test]
fn changed_paths_list_every_path_of_a_large_train_and_both_ends_of_a_move() {
    let dir = tempfile::tempdir().unwrap();
    let repo = dir.path();
    git(repo, &["init", "-q", "-b", "main"]);
    std::fs::create_dir_all(repo.join("tools")).unwrap();
    std::fs::write(repo.join("tools/ci_commit_gate.py"), "gate\n").unwrap();
    std::fs::write(repo.join("keep.txt"), "keep\n").unwrap();
    git(repo, &["add", "-A"]);
    git(repo, &["commit", "-q", "-m", "base"]);
    let base = String::from_utf8(
        std::process::Command::new("git")
            .args(["rev-parse", "HEAD"])
            .current_dir(repo)
            .output()
            .unwrap()
            .stdout,
    )
    .unwrap()
    .trim()
    .to_owned();
    // More files than the compare API lists (300), and the protected file moved away at the end.
    std::fs::create_dir_all(repo.join("many")).unwrap();
    for n in 0..350 {
        std::fs::write(repo.join(format!("many/f{n:03}.txt")), format!("{n}\n")).unwrap();
    }
    std::fs::create_dir_all(repo.join("docs")).unwrap();
    git(repo, &["mv", "tools/ci_commit_gate.py", "docs/gate.txt"]);
    git(repo, &["add", "-A"]);
    git(repo, &["commit", "-q", "-m", "train"]);
    let head = String::from_utf8(
        std::process::Command::new("git")
            .args(["rev-parse", "HEAD"])
            .current_dir(repo)
            .output()
            .unwrap()
            .stdout,
    )
    .unwrap()
    .trim()
    .to_owned();
    let paths = super::go_evidence::changed_paths(repo, &base, &head).unwrap();
    assert_eq!(paths.len(), 352);
    assert!(
        paths.contains(&"tools/ci_commit_gate.py".to_owned()),
        "the old end of the move"
    );
    assert!(paths.contains(&"docs/gate.txt".to_owned()));
    assert!(paths.contains(&"many/f349.txt".to_owned()));
    assert!(!paths.contains(&"keep.txt".to_owned()));
}

#[test]
fn a_decisions_document_larger_than_a_mebibyte_is_read_whole() {
    let dir = tempfile::tempdir().unwrap();
    let (origin, clone) = (dir.path().join("origin.git"), dir.path().join("clone"));
    git(
        dir.path(),
        &[
            "init",
            "-q",
            "--bare",
            "-b",
            "main",
            origin.to_str().unwrap(),
        ],
    );
    git(
        dir.path(),
        &[
            "clone",
            "-q",
            origin.to_str().unwrap(),
            clone.to_str().unwrap(),
        ],
    );
    let big: String = (0..30_000).map(|n| format!("| D-{n} | name | decision text of row {n} padded out to a longer line | router | |\n")).collect();
    assert!(big.len() > 2 * 1024 * 1024);
    std::fs::write(clone.join("DECISIONS.md"), &big).unwrap();
    git(&clone, &["add", "-A"]);
    git(&clone, &["commit", "-q", "-m", "decisions"]);
    git(&clone, &["push", "-q", "origin", "HEAD:refs/heads/main"]);
    assert_eq!(
        read_decisions_origin(&clone.join("DECISIONS.md")).unwrap(),
        big
    );
}
