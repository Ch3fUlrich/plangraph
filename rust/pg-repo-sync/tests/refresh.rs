//! Real Git merges; only GitHub and the orchestrator are fixtures.
#![cfg(unix)]
use std::{fs, path::Path, process::Command};

fn git(repo: &Path, args: &[&str]) -> String {
    let out = Command::new("git")
        .current_dir(repo)
        .env("FIXTURE", repo.parent().unwrap())
        .args([
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
        ])
        .args(args)
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    String::from_utf8(out.stdout).unwrap().trim().into()
}
fn script(path: &Path, body: &str) {
    pg_worker_core::executable_test_support::write_executable(
        path,
        format!("#!/bin/sh\nset -eu\n{body}\n"),
    );
}
fn commit(repo: &Path, message: &str) {
    git(repo, &["add", "."]);
    git(repo, &["commit", "-qm", message]);
}

#[test]
fn probe_labels_a_real_conflict_and_never_remerges_a_behind_head() {
    let tmp = tempfile::tempdir().unwrap();
    let root = tmp.path();
    let remote = root.join("remote.git");
    let repo = root.join("repo");
    let bin = root.join("bin");
    fs::create_dir(&bin).unwrap();
    git(root, &["init", "--bare", "-q", remote.to_str().unwrap()]);
    git(
        root,
        &[
            "clone",
            "-q",
            remote.to_str().unwrap(),
            repo.to_str().unwrap(),
        ],
    );
    git(&repo, &["checkout", "-qb", "main"]);
    fs::write(repo.join(".gitattributes"), "CHANGELOG.md merge=union\n").unwrap();
    fs::write(repo.join("CHANGELOG.md"), "base\n").unwrap();
    fs::write(repo.join("code.txt"), "base\n").unwrap();
    commit(&repo, "base");
    git(&repo, &["push", "-q", "origin", "main"]);
    git(&repo, &["checkout", "-qb", "union"]);
    fs::write(repo.join("CHANGELOG.md"), "base\npr append\n").unwrap();
    commit(&repo, "pr append");
    git(&repo, &["push", "-q", "origin", "union"]);
    let union = git(&repo, &["rev-parse", "HEAD"]);
    git(&repo, &["checkout", "-qb", "conflict", "main"]);
    fs::write(repo.join("code.txt"), "pr edit\n").unwrap();
    commit(&repo, "pr edit");
    git(&repo, &["push", "-q", "origin", "conflict"]);
    let conflict = git(&repo, &["rev-parse", "HEAD"]);
    git(&repo, &["checkout", "-q", "main"]);
    fs::write(repo.join("CHANGELOG.md"), "base\nmain append\n").unwrap();
    fs::write(repo.join("code.txt"), "main edit\n").unwrap();
    commit(&repo, "main advances");
    git(&repo, &["push", "-q", "origin", "main"]);
    let main = git(&repo, &["rev-parse", "HEAD"]);
    let prs = root.join("prs.json");
    fs::write(
        &prs,
        format!(
            r#"[
        {{"number":1,"headRefName":"union","headRefOid":"{union}","isCrossRepository":false}},
        {{"number":2,"headRefName":"conflict","headRefOid":"{conflict}","isCrossRepository":false}}
    ]"#
        ),
    )
    .unwrap();
    script(
        &bin.join("gh"),
        r#"
case "$*" in
  'pr list --repo owner/repo --state open --label batch-ready '*) cat "$FIXTURE/prs.json";;
  'api --method POST repos/owner/repo/issues/2/labels --raw-field labels[]=needs-update') echo "$*" >> "$FIXTURE/labels";;
  *) echo "unexpected gh: $*" >&2; exit 2;;
esac"#,
    );
    script(&bin.join("notify"), "cat >> \"$FIXTURE/alerts\"");
    script(
        &repo.join(".git/hooks/pre-push"),
        "echo push >> \"$FIXTURE/pushes\"",
    );
    script(
        &repo.join(".git/hooks/pre-merge-commit"),
        "echo merge >> \"$FIXTURE/merges\"",
    );
    let invoke = || {
        Command::new(env!("CARGO_BIN_EXE_pg-repo-sync"))
            .args([
                "refresh-prs",
                "--repo",
                repo.to_str().unwrap(),
                "--github",
                "owner/repo",
                "--notify",
            ])
            .arg(bin.join("notify"))
            .env(
                "PATH",
                format!("{}:{}", bin.display(), std::env::var("PATH").unwrap()),
            )
            .env("FIXTURE", root)
            .env("GIT_AUTHOR_NAME", "Fixture")
            .env("GIT_AUTHOR_EMAIL", "fixture@example.invalid")
            .env("GIT_COMMITTER_NAME", "Fixture")
            .env("GIT_COMMITTER_EMAIL", "fixture@example.invalid")
            .output()
            .unwrap()
    };
    let out = invoke();
    assert_eq!(out.status.code(), Some(1), "{out:?}");
    // A behind part that merges cleanly is not re-merged with main: its head stays as the writer pushed it.
    assert_eq!(git(&remote, &["rev-parse", "union"]), union);
    assert_eq!(git(&remote, &["rev-parse", "conflict"]), conflict);
    assert!(fs::read_to_string(root.join("labels"))
        .unwrap()
        .contains("needs-update"));
    assert!(fs::read_to_string(root.join("alerts"))
        .unwrap()
        .contains("#2 conflict code.txt"));
    assert!(!root.join("pushes").exists(), "the probe never pushes");
    assert_eq!(fs::read_to_string(root.join("merges")).unwrap(), "merge\n");
    assert_eq!(git(&repo, &["status", "--porcelain"]), "");
    assert_eq!(git(&repo, &["rev-parse", "HEAD"]), main);
    assert_eq!(
        git(&repo, &["worktree", "list", "--porcelain"])
            .matches("worktree ")
            .count(),
        1
    );

    // Without the conflicting PR a sweep succeeds.
    fs::write(&prs, format!(r#"[{{"number":1,"headRefName":"union","headRefOid":"{union}","isCrossRepository":false}}]"#)).unwrap();
    assert!(invoke().status.success());
    // The normal sync entry point probes PRs after fast-forwarding a new main and still pushes nothing.
    fs::write(repo.join("CHANGELOG.md"), "base\nmain append\nnext main\n").unwrap();
    commit(&repo, "next main");
    git(&repo, &["push", "-q", "origin", "main"]);
    let next_main = git(&repo, &["rev-parse", "HEAD"]);
    git(&repo, &["reset", "--hard", &main]);
    script(&bin.join("systemctl"), "exit 0");
    let out = Command::new(env!("CARGO_BIN_EXE_pg-repo-sync"))
        .args([
            "--repo",
            repo.to_str().unwrap(),
            "--refresh-prs",
            "owner/repo",
            "--notify",
        ])
        .arg(bin.join("notify"))
        .env(
            "PATH",
            format!("{}:{}", bin.display(), std::env::var("PATH").unwrap()),
        )
        .env("FIXTURE", root)
        .env("GIT_AUTHOR_NAME", "Fixture")
        .env("GIT_AUTHOR_EMAIL", "fixture@example.invalid")
        .env("GIT_COMMITTER_NAME", "Fixture")
        .env("GIT_COMMITTER_EMAIL", "fixture@example.invalid")
        .output()
        .unwrap();
    assert!(out.status.success(), "{out:?}");
    assert_eq!(git(&repo, &["rev-parse", "HEAD"]), next_main);
    assert_eq!(git(&remote, &["rev-parse", "union"]), union);
    assert_eq!(
        fs::read_to_string(root.join("pushes")).unwrap(),
        "push\n",
        "only the fixture's own push of the next main: the probe never pushes"
    );
    assert_eq!(
        fs::read_to_string(root.join("labels"))
            .unwrap()
            .lines()
            .count(),
        1
    );
    assert_eq!(
        git(&repo, &["worktree", "list", "--porcelain"])
            .matches("worktree ")
            .count(),
        1
    );
}
