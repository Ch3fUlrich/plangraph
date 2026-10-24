//! Test helpers: throwaway git repositories.

use crate::git::git;
use std::path::Path;
use tempfile::TempDir;

/// A temp repo with one commit on `main`; returns (dir, base sha).
pub fn repo() -> (TempDir, String) {
    let dir = tempfile::tempdir().unwrap();
    let p = dir.path();
    git(p, &["init", "-q", "-b", "main"]).unwrap();
    git(p, &["config", "user.email", "t@example.com"]).unwrap();
    git(p, &["config", "user.name", "tester"]).unwrap();
    git(p, &["config", "commit.gpgsign", "false"]).unwrap();
    write(p, "README.md", "hello\n");
    let sha = commit_all(p, "init");
    (dir, sha)
}

pub fn write(dir: &Path, rel: &str, body: &str) {
    let f = dir.join(rel);
    std::fs::create_dir_all(f.parent().unwrap()).unwrap();
    std::fs::write(f, body).unwrap();
}

pub fn commit_all(dir: &Path, msg: &str) -> String {
    git(dir, &["add", "-A"]).unwrap();
    git(dir, &["commit", "-q", "-m", msg]).unwrap();
    git(dir, &["rev-parse", "HEAD"]).unwrap()
}
