//! Part-entry predicate: imported_history, undeclared_dependency, shared_history.
use std::path::Path;
use std::process::Command;

fn git(repo: &Path, args: &[&str]) -> String {
    let output = Command::new("git")
        .current_dir(repo)
        .args([
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
        ])
        .args(args)
        .output()
        .unwrap();
    assert!(output.status.success(), "{:?}", output);
    String::from_utf8(output.stdout).unwrap().trim().into()
}

fn commit(repo: &Path, file: &str) -> String {
    std::fs::write(repo.join(file), file).unwrap();
    git(repo, &["add", file]);
    git(repo, &["commit", "-qm", file]);
    git(repo, &["rev-parse", "HEAD"])
}

fn branch(repo: &Path, name: &str, from: &str) {
    git(repo, &["checkout", "-q", "-B", name, from]);
}

/// (stdout, success) of the predicate for `head` with an optional parent head and other open heads.
fn check(
    repo: &Path,
    base: &str,
    head: &str,
    parent: Option<&str>,
    others: &[&str],
) -> (String, bool) {
    let mut args = vec!["--part-entry", base, head, parent.unwrap_or("-")];
    args.extend_from_slice(others);
    let out = Command::new(env!("CARGO_BIN_EXE_pg-policy"))
        .current_dir(repo)
        .args(args)
        .output()
        .unwrap();
    (String::from_utf8(out.stdout).unwrap(), out.status.success())
}

struct Fixture {
    dir: tempfile::TempDir,
    main: String,
}

fn fixture() -> Fixture {
    let dir = tempfile::tempdir().unwrap();
    git(dir.path(), &["init", "-q", "-b", "main"]);
    let main = commit(dir.path(), "base.txt");
    Fixture { dir, main }
}

#[test]
fn a_clean_bottom_part_is_accepted() {
    let f = fixture();
    let r = f.dir.path();
    branch(r, "a", &f.main);
    let a = commit(r, "a.txt");
    assert_eq!(check(r, &f.main, &a, None, &[]), (String::new(), true));
}

#[test]
fn a_merge_of_a_foreign_branch_is_imported_history() {
    let f = fixture();
    let r = f.dir.path();
    branch(r, "foreign", &f.main);
    let foreign = commit(r, "foreign.txt");
    branch(r, "a", &f.main);
    commit(r, "a.txt");
    git(r, &["merge", "--no-ff", "-q", &foreign, "-m", "import"]);
    let a = git(r, &["rev-parse", "HEAD"]);
    let (out, ok) = check(r, &f.main, &a, None, &[]);
    assert!(!ok);
    assert!(out.starts_with("refused imported_history"), "{out}");
}

#[test]
fn a_merge_of_main_or_of_the_parent_head_is_allowed() {
    let f = fixture();
    let r = f.dir.path();
    branch(r, "parent", &f.main);
    let q = commit(r, "parent.txt");
    branch(r, "child", &f.main);
    commit(r, "child.txt");
    git(r, &["merge", "--no-ff", "-q", &q, "-m", "take the parent"]);
    let child = git(r, &["rev-parse", "HEAD"]);
    assert_eq!(
        check(r, &f.main, &child, Some(&q), &[]),
        (String::new(), true)
    );
    // the same merge without naming the parent imports history
    assert!(!check(r, &f.main, &child, None, &[]).1);
    // a merge of main itself is fine for a bottom part
    branch(r, "later-main", &f.main);
    let newer = commit(r, "newer.txt");
    branch(r, "bottom", &f.main);
    commit(r, "bottom.txt");
    git(r, &["merge", "--no-ff", "-q", &newer, "-m", "refresh"]);
    let bottom = git(r, &["rev-parse", "HEAD"]);
    assert_eq!(check(r, &newer, &bottom, None, &[]), (String::new(), true));
}

#[test]
fn building_on_another_open_head_without_saying_so_is_undeclared_dependency() {
    let f = fixture();
    let r = f.dir.path();
    branch(r, "z", &f.main);
    let z = commit(r, "z.txt");
    branch(r, "x", &z);
    let x = commit(r, "x.txt");
    let (out, ok) = check(r, &f.main, &x, None, &[&z]);
    assert!(!ok);
    assert!(out.starts_with("refused undeclared_dependency"), "{out}");
    // declared as the parent: the caller passes it as parent, not as an other head
    assert_eq!(check(r, &f.main, &x, Some(&z), &[]), (String::new(), true));
}

#[test]
fn an_open_head_already_in_main_is_not_a_dependency() {
    let f = fixture();
    let r = f.dir.path();
    branch(r, "x", &f.main);
    let x = commit(r, "x.txt");
    // landed: the base moved onto z, and z is contained in the base
    branch(r, "z", &f.main);
    let z = commit(r, "z.txt");
    assert!(check(r, &z, &x, None, &[&z]).1);
}

#[test]
fn a_head_that_contains_the_candidate_is_the_others_problem_not_the_candidates() {
    let f = fixture();
    let r = f.dir.path();
    branch(r, "x", &f.main);
    let x = commit(r, "x.txt");
    branch(r, "z", &x);
    let z = commit(r, "z.txt");
    assert_eq!(check(r, &f.main, &x, None, &[&z]), (String::new(), true));
}

#[test]
fn two_heads_that_share_commits_outside_main_are_shared_history() {
    let f = fixture();
    let r = f.dir.path();
    branch(r, "shared", &f.main);
    let shared = commit(r, "shared.txt");
    branch(r, "x", &shared);
    let x = commit(r, "x.txt");
    branch(r, "z", &shared);
    let z = commit(r, "z.txt");
    let (out, ok) = check(r, &f.main, &x, None, &[&z]);
    assert!(!ok);
    assert!(out.starts_with("refused shared_history"), "{out}");
}

#[test]
fn unrelated_parts_over_main_do_not_share_history() {
    let f = fixture();
    let r = f.dir.path();
    branch(r, "x", &f.main);
    let x = commit(r, "x.txt");
    branch(r, "z", &f.main);
    let z = commit(r, "z.txt");
    assert_eq!(check(r, &f.main, &x, None, &[&z]), (String::new(), true));
}

#[test]
fn an_equal_head_is_left_to_the_duplicate_rule_and_is_neither_shared_nor_undeclared() {
    let f = fixture();
    let r = f.dir.path();
    branch(r, "x", &f.main);
    let x = commit(r, "x.txt");
    assert_eq!(check(r, &f.main, &x, None, &[&x]), (String::new(), true));
}

#[test]
fn a_bad_revision_is_an_error_not_a_pass() {
    let f = fixture();
    let (out, ok) = check(
        f.dir.path(),
        &f.main,
        "0000000000000000000000000000000000000001",
        None,
        &[],
    );
    assert!(!ok);
    assert!(out.contains("part-entry"), "{out}");
}
