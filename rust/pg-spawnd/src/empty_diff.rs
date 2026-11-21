//! A review card whose diff is empty fails once, before routing or any leg is tried.
use crate::errors::{RefusalKind, ToolError};
use crate::placement::Args;
use pg_worker_core::hosts::RepoEntry;
use pg_worker_core::workspace_resolver;
use serde_json::Value;
use std::collections::BTreeMap;
use std::path::Path;
use std::process::Stdio;

fn plain(value: &Value) -> Option<&str> {
    value
        .as_str()
        .filter(|s| !s.is_empty() && !s.starts_with('-') && !s.chars().any(char::is_whitespace))
}

/// `Err(Refused empty_diff)` when role review names a `diff_ref` {repo, base, head[, paths]}
/// that changes nothing. A diff that cannot be read here is left to the legs.
pub(crate) fn check(args: &Args) -> Result<(), ToolError> {
    check_with(args, &crate::prompt::repos())
}

fn check_with(args: &Args, repos: &BTreeMap<String, RepoEntry>) -> Result<(), ToolError> {
    if args.get("role").and_then(Value::as_str) != Some("review") {
        return Ok(());
    }
    let reference = &args["card"]["diff_ref"];
    // Two formats: {repo, base, head} (a path) and the registry-rooted {repo_id, base_sha, head_sha}.
    let registered = reference["repo_id"]
        .as_str()
        .and_then(|id| repos.get(id).map(|entry| entry.root.clone()));
    let (root, base, head) = match registered {
        Some(root) => (Some(root), &reference["base_sha"], &reference["head_sha"]),
        None => (
            reference["repo"]
                .as_str()
                .filter(|r| Path::new(r).is_absolute())
                .map(str::to_owned),
            &reference["base"],
            &reference["head"],
        ),
    };
    let (Some(repo), Some(base), Some(head)) = (root.as_deref(), plain(base), plain(head)) else {
        return Ok(());
    };
    let mut cmd = workspace_resolver::git_command(Path::new(repo));
    cmd.args([
        "--literal-pathspecs",
        "diff",
        "--quiet",
        "--no-ext-diff",
        "--no-textconv",
    ])
    .arg(format!("{base}...{head}"))
    .arg("--");
    if let Some(paths) = reference["paths"].as_array() {
        cmd.args(paths.iter().filter_map(Value::as_str));
    }
    let status = cmd.stdout(Stdio::null()).stderr(Stdio::null()).status();
    if status.is_ok_and(|s| s.code() == Some(0)) {
        return Err(ToolError::Refused {
            kind: RefusalKind::Spawn,
            message: format!("empty_diff: no change between base {base} and head {head}"),
            legs: vec![],
        });
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::process::Command;

    fn git(dir: &Path, args: &[&str]) -> String {
        let out = Command::new("git")
            .arg("-C")
            .arg(dir)
            .args([
                "-c",
                "user.name=t",
                "-c",
                "user.email=t@example.invalid",
                "-c",
                "commit.gpgsign=false",
            ])
            .args(args)
            .output()
            .unwrap();
        assert!(out.status.success(), "{args:?}");
        String::from_utf8(out.stdout).unwrap().trim().to_owned()
    }

    fn card(repo: &Path, base: &str, head: &str, role: &str) -> Args {
        json!({"role": role, "card": {"diff_ref": {"repo": repo, "base": base, "head": head}}})
            .as_object()
            .unwrap()
            .clone()
    }

    #[test]
    fn same_head_fails_once_with_empty_diff_and_a_real_diff_passes() {
        let dir = tempfile::tempdir().unwrap();
        git(dir.path(), &["init", "-q", "-b", "main"]);
        std::fs::write(dir.path().join("a.txt"), "one\n").unwrap();
        git(dir.path(), &["add", "a.txt"]);
        git(dir.path(), &["commit", "-qm", "base"]);
        let base = git(dir.path(), &["rev-parse", "HEAD"]);
        std::fs::write(dir.path().join("a.txt"), "two\n").unwrap();
        git(dir.path(), &["commit", "-qam", "head"]);
        let head = git(dir.path(), &["rev-parse", "HEAD"]);
        assert!(check(&card(dir.path(), &base, &head, "review")).is_ok());
        match check(&card(dir.path(), &head, &head, "review")) {
            Err(ToolError::Refused { message, .. }) => assert!(message.starts_with("empty_diff:")),
            _ => panic!("same head must be refused"),
        }
        // Only a review card is checked; an unreadable diff is left to the legs.
        assert!(check(&card(dir.path(), &head, &head, "implement")).is_ok());
        assert!(check(&card(dir.path(), &"0".repeat(40), &head, "review")).is_ok());
        assert!(check(&card(&dir.path().join("missing"), &head, &head, "review")).is_ok());
    }

    #[test]
    fn registry_rooted_diff_refs_are_checked_too() {
        let dir = tempfile::tempdir().unwrap();
        git(dir.path(), &["init", "-q", "-b", "main"]);
        std::fs::write(dir.path().join("a.txt"), "one\n").unwrap();
        git(dir.path(), &["add", "a.txt"]);
        git(dir.path(), &["commit", "-qm", "base"]);
        let head = git(dir.path(), &["rev-parse", "HEAD"]);
        let entry: RepoEntry = serde_json::from_value(json!({
            "root": dir.path(), "worktrees_root": dir.path(), "remote": "origin",
            "import_roots": [], "cache_env": {}
        }))
        .unwrap();
        let repos = BTreeMap::from([("demo".to_owned(), entry)]);
        let card = |base: &str| -> Args {
            json!({"role": "review", "card": {"diff_ref": {"repo_id": "demo", "base_sha": base, "head_sha": head}}})
                .as_object()
                .unwrap()
                .clone()
        };
        match check_with(&card(&head), &repos) {
            Err(ToolError::Refused { message, .. }) => assert!(message.starts_with("empty_diff:")),
            _ => panic!("an empty registry-rooted range must be refused"),
        }
        // An unregistered repo id is left to the legs.
        assert!(check_with(&card(&head), &BTreeMap::new()).is_ok());
    }
}
