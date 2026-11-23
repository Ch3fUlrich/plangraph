//! Real Git merges and pushes; only GitHub and the orchestrator are fixtures.
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
fn refresh_union_pushes_real_conflict_labels_without_push() {
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
    assert!(
        String::from_utf8_lossy(&out.stdout).contains("REFRESHED #1"),
        "{out:?}"
    );
    let refreshed = git(&remote, &["rev-parse", "union"]);
    assert_ne!(refreshed, union);
    assert_eq!(git(&remote, &["rev-parse", "union^1"]), union);
    assert_eq!(git(&remote, &["rev-parse", "union^2"]), main);
    let content = git(&remote, &["show", "union:CHANGELOG.md"]);
    assert!(content.contains("pr append") && content.contains("main append"));
    assert_eq!(git(&remote, &["rev-parse", "conflict"]), conflict);
    assert!(fs::read_to_string(root.join("labels"))
        .unwrap()
        .contains("needs-update"));
    assert!(fs::read_to_string(root.join("alerts"))
        .unwrap()
        .contains("#2 conflict code.txt"));
    assert_eq!(fs::read_to_string(root.join("pushes")).unwrap(), "push\n");
    assert_eq!(fs::read_to_string(root.join("merges")).unwrap(), "merge\n");
    assert_eq!(git(&repo, &["status", "--porcelain"]), "");
    assert_eq!(git(&repo, &["rev-parse", "HEAD"]), main);
    assert_eq!(
        git(&repo, &["worktree", "list", "--porcelain"])
            .matches("worktree ")
            .count(),
        1
    );

    // Update the API snapshot, then a second sweep does not push an up-to-date PR.
    fs::write(&prs, format!(r#"[{{"number":1,"headRefName":"union","headRefOid":"{refreshed}","isCrossRepository":false}}]"#)).unwrap();
    assert!(invoke().status.success());
    // The normal sync entry point refreshes PRs after fast-forwarding a new main.
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
    assert_eq!(git(&remote, &["rev-parse", "union^2"]), next_main);
    assert_eq!(
        fs::read_to_string(root.join("pushes")).unwrap(),
        "push\npush\npush\n"
    );
    let refreshed = git(&remote, &["rev-parse", "union"]);
    fs::write(&prs, format!(r#"[{{"number":1,"headRefName":"union","headRefOid":"{refreshed}","isCrossRepository":false}}]"#)).unwrap();
    // A hook refusal is an error, never a conflict label or an unhooked push.
    fs::write(
        repo.join("CHANGELOG.md"),
        "base\nmain append\nnext main\nthird main\n",
    )
    .unwrap();
    commit(&repo, "third main");
    git(&repo, &["push", "-q", "origin", "main"]);
    script(&repo.join(".git/hooks/pre-push"), "exit 1");
    let out = invoke();
    assert_eq!(out.status.code(), Some(1), "{out:?}");
    assert_eq!(git(&remote, &["rev-parse", "union"]), refreshed);
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
