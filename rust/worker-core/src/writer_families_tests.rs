use super::*;
use crate::git_test_support::command;

fn git(repo: &Path, args: &[&str]) -> String {
    let out = command(repo).args(args).output().unwrap();
    assert!(out.status.success(), "{args:?}: {out:?}");
    String::from_utf8_lossy(&out.stdout).trim().to_owned()
}
fn commit(repo: &Path, name: &str, message: &str) {
    std::fs::write(repo.join(name), message).unwrap();
    git(repo, &["add", name]);
    git(
        repo,
        &[
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-q",
            "-m",
            message,
        ],
    );
}
fn python() -> Command {
    // The checkout holding src/plangraph is two levels above this crate.
    python_command(Some(&Path::new(env!("CARGO_MANIFEST_DIR")).join("../..")))
}

#[test]
fn every_writer_family_of_the_range_comes_from_its_trailers() {
    let dir = tempfile::tempdir().unwrap();
    let repo = dir.path();
    git(repo, &["init", "-q", "-b", "main"]);
    commit(repo, "base.txt", "base");
    let base = git(repo, &["rev-parse", "HEAD"]);
    commit(
        repo,
        "a.txt",
        "a\n\nWriter: gemini-4-hydrogen-high run-6f00f4b3",
    );
    commit(
        repo,
        "b.txt",
        "b\n\nWriter: gpt-6.2-sol-medium run-0ad2ce51",
    );
    commit(repo, "c.txt", "c\n\nHand-Edit: changelog");
    let head = git(repo, &["rev-parse", "HEAD"]);
    let families = writer_families(repo, &base, &head, &[], &python).unwrap();
    assert_eq!(families, ["google", "openai"]);
    // A serving model that wrote without a trailer joins the set.
    let families = writer_families(repo, &base, &head, &["claude-sonnet-5-5"], &python).unwrap();
    assert_eq!(families, ["anthropic", "google", "openai"]);
    // A range whose commits name no family is decided by the leg models alone.
    commit(repo, "d.txt", "d");
    let head = git(repo, &["rev-parse", "HEAD"]);
    let prior = git(repo, &["rev-parse", "HEAD~1"]);
    assert!(writer_families(repo, &prior, &head, &[], &python).is_err());
    assert_eq!(
        writer_families(repo, &prior, &head, &["gpt-6.2-sol"], &python).unwrap(),
        ["openai"]
    );
    assert!(
        writer_families(repo, &prior, &head, &["no-such-model"], &python)
            .unwrap_err()
            .contains("unknown writer family of model no-such-model")
    );
}
