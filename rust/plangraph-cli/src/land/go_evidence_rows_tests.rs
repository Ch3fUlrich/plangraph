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
