use super::stamp::{embedded_inputs, embedded_literals, release_stamp};
use std::{fs, path::Path, process::Command};

fn git(repo: &Path, args: &[&str]) {
    let out = Command::new("git")
        .arg("-C")
        .arg(repo)
        .args(["-c", "user.name=F", "-c", "user.email=f@example.invalid"])
        .args(args)
        .output()
        .unwrap();
    assert!(out.status.success(), "{out:?}");
}

#[test]
fn the_literals_after_include_macros_are_found_across_lines_and_kinds() {
    let src = "const A: &str = include_str!(\"../a.toml\");\nstatic B: &[u8] = include_bytes!(\n    \"../../b.bin\"\n);\nlet n = include_str!(concat!(\"x\"));";
    assert_eq!(embedded_literals(src), ["../a.toml", "../../b.bin"]);
}

#[test]
fn only_inputs_outside_the_rust_tree_are_listed_and_they_change_the_stamp() {
    let dir = tempfile::tempdir().unwrap();
    let repo = dir.path();
    fs::create_dir_all(repo.join("rust/a/src")).unwrap();
    fs::create_dir_all(repo.join("rust/a/data")).unwrap();
    fs::create_dir_all(repo.join("deploy")).unwrap();
    fs::write(
        repo.join("rust/a/src/lib.rs"),
        "const X: &str = include_str!(\"../../../deploy/e.toml\");\nconst Y: &str = include_str!(\"../data/in.txt\");\n",
    )
    .unwrap();
    fs::write(repo.join("rust/a/data/in.txt"), "inside\n").unwrap();
    fs::write(repo.join("deploy/e.toml"), "one\n").unwrap();
    git(repo, &["init", "-q"]);
    git(repo, &["add", "-A"]);
    git(repo, &["commit", "-qm", "one"]);
    assert_eq!(embedded_inputs(repo).unwrap(), ["deploy/e.toml"]);
    let (first, inputs) = release_stamp(repo).unwrap();
    assert_eq!(inputs, ["deploy/e.toml"]);
    assert_eq!(release_stamp(repo).unwrap().0, first, "the stamp is stable");
    fs::write(repo.join("deploy/e.toml"), "two\n").unwrap();
    git(repo, &["commit", "-qam", "two"]);
    assert_ne!(
        release_stamp(repo).unwrap().0,
        first,
        "an embedded file changed"
    );
}

#[test]
fn a_tree_that_embeds_nothing_outside_keeps_the_plain_tree_stamp() {
    let dir = tempfile::tempdir().unwrap();
    let repo = dir.path();
    fs::create_dir_all(repo.join("rust")).unwrap();
    fs::write(repo.join("rust/x.rs"), "fn main() {}\n").unwrap();
    git(repo, &["init", "-q"]);
    git(repo, &["add", "-A"]);
    git(repo, &["commit", "-qm", "one"]);
    let tree = String::from_utf8(
        Command::new("git")
            .arg("-C")
            .arg(repo)
            .args(["rev-parse", "HEAD:rust"])
            .output()
            .unwrap()
            .stdout,
    )
    .unwrap();
    assert_eq!(release_stamp(repo).unwrap().0, tree.trim());
}
