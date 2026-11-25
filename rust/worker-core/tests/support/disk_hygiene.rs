//! Shared fixtures of the disk-hygiene CLI tests: a repository with linked worktrees to sweep.
use pg_worker_core::host_command;
use std::{fs, path::Path, time::Duration};

pub fn git(repo: &Path, args: &[&str]) {
    host_command::output(
        pg_worker_core::git_test_support::command(repo)
            .args([
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=test@example.invalid",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "commit.gpgsign=false",
            ])
            .args(args),
        Duration::from_secs(10),
    )
    .unwrap();
}
pub fn fixture() -> (tempfile::TempDir, std::path::PathBuf, std::path::PathBuf) {
    let tmp = tempfile::tempdir().unwrap();
    let repo = tmp.path().join("main");
    let root = tmp.path().join("trees");
    fs::create_dir(&repo).unwrap();
    fs::create_dir(&root).unwrap();
    git(&repo, &["init", "-qb", "main"]);
    fs::write(repo.join(".gitignore"), "target/\nnode_modules/\n").unwrap();
    git(&repo, &["add", "."]);
    git(&repo, &["commit", "-qm", "one"]);
    git(&repo, &["update-ref", "refs/remotes/origin/main", "HEAD"]);
    for name in ["pinned", "dirty", "live", "clean"] {
        git(
            &repo,
            &[
                "worktree",
                "add",
                "-qb",
                name,
                root.join(name).to_str().unwrap(),
            ],
        );
        fs::create_dir(root.join(name).join("target")).unwrap();
        fs::write(root.join(name).join("target/cache"), [0u8; 64]).unwrap();
    }
    fs::write(root.join("dirty/untracked"), "valuable").unwrap();
    (tmp, repo, root)
}
