use super::paths::read_text;
use crate::batch_writer_brief::{self, Input};
use pg_worker_core::writer_brief::PushedFix;
use serde_json::Value;
use std::{borrow::Cow, io, path::Path, time::Duration};

#[derive(Clone)]
pub(super) struct WriterPrompt {
    pub text: String,
    pub wire: Option<String>,
    pub pushed_fix: Option<PushedFix>,
}

pub(super) fn read_writer_prompt(path: &Path, policy: Option<&Value>) -> io::Result<WriterPrompt> {
    let text = read_text(path)?;
    let input = Input::decode(&text).map_err(io::Error::other)?;
    if let Some(input) = input {
        let policy =
            policy.ok_or_else(|| io::Error::other("effective batch writer policy missing"))?;
        let prompt = batch_writer_brief::compose(&input, "claude", false, Some(policy))
            .map_err(io::Error::other)?;
        return Ok(WriterPrompt {
            text: prompt,
            wire: Some(text),
            pushed_fix: input.pushed_fix,
        });
    }
    // V1 remains available for pre-cutover senders; typed forwarding always carries wire.
    let text = match batch_writer_brief::render(&text).map_err(io::Error::other)? {
        Cow::Borrowed(_) => text,
        Cow::Owned(brief) => {
            const SKILL: &str = include_str!("../../../../skills/batch-writer/SKILL.md");
            crate::role_header::compose("claude", &brief, Some(SKILL))
        }
    };
    Ok(WriterPrompt {
        text,
        wire: None,
        pushed_fix: None,
    })
}

/// Move a clean Claude checkout forward to the exact head still advertised by its authorized
/// remote lane. The native resume path keeps its local head instead.
pub(super) fn reset_pushed_fix(cwd: &Path, fix: &PushedFix) -> Result<(), String> {
    let git = |args: &[&str]| {
        #[cfg(test)]
        let mut command = pg_worker_core::git_test_support::command(cwd);
        #[cfg(not(test))]
        let mut command = std::process::Command::new("git");
        let command = command
            .current_dir(cwd)
            .args(args)
            .env_remove("GIT_DIR")
            .env_remove("GIT_WORK_TREE")
            .env_remove("GIT_INDEX_FILE")
            .env_remove("GIT_COMMON_DIR")
            .env_remove("GIT_NAMESPACE")
            .env("GIT_TERMINAL_PROMPT", "0");
        let output = super::process::capture(command, Some(b""), Duration::from_secs(30))
            .map_err(|e| format!("git {}: {e}", args[0]))?;
        if !output.status.success() {
            return Err(format!(
                "git {}: {}",
                args[0],
                String::from_utf8_lossy(&output.stderr).trim()
            ));
        }
        String::from_utf8(output.stdout).map_err(|e| e.to_string())
    };
    if std::fs::canonicalize(git(&["rev-parse", "--show-toplevel"])?.trim_end()).ok()
        != Some(cwd.canonicalize().map_err(|error| error.to_string())?)
    {
        return Err("Claude fix round requires a checkout root".into());
    }
    git(&["check-ref-format", &fix.reference])?;
    let advertised = git(&[
        "ls-remote",
        "--exit-code",
        "--refs",
        "origin",
        &fix.reference,
    ])?;
    if advertised.trim_end() != format!("{}\t{}", fix.sha, fix.reference) {
        return Err("authorized pushed SHA differs from the origin lane".into());
    }
    if git(&["cat-file", "-t", &fix.sha]).as_deref() != Ok("commit\n") {
        git(&["fetch", "--no-write-fetch-head", "origin", &fix.sha])?;
    }
    if !git(&["status", "--porcelain=v1", "--untracked-files=all"])?.is_empty() {
        return Err("Claude fix round requires a clean worktree".into());
    }
    git(&["merge-base", "--is-ancestor", "HEAD", &fix.sha])
        .map_err(|_| "local HEAD contains commits outside the authorized pushed head".to_owned())?;
    git(&["reset", "--hard", &fix.sha])?;
    Ok(())
}

#[cfg(test)]
#[test]
fn pushed_fix_checks_remote_sha_cleanliness_and_ancestry() {
    use std::fs;
    fn git(root: &Path, args: &[&str]) -> String {
        let output = pg_worker_core::git_test_support::command(root)
            .args(args)
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{args:?}: {}",
            String::from_utf8_lossy(&output.stderr)
        );
        String::from_utf8(output.stdout)
            .unwrap()
            .trim_end()
            .to_owned()
    }
    let temp = tempfile::tempdir().unwrap();
    let root = temp.path();
    let origin = root.join("origin");
    git(root, &["init", "-b", "main", "origin"]);
    git(&origin, &["commit", "--allow-empty", "-m", "base"]);
    git(root, &["clone", "origin", "writer"]);
    let writer = root.join("writer");
    let base = git(&writer, &["rev-parse", "HEAD"]);
    git(&origin, &["commit", "--allow-empty", "-m", "new"]);
    git(&origin, &["branch", "lane"]);
    let sha = git(&origin, &["rev-parse", "HEAD"]);
    let mut fix = PushedFix {
        reference: "refs/heads/lane".into(),
        sha: base.clone(),
    };
    assert!(reset_pushed_fix(&writer, &fix)
        .unwrap_err()
        .contains("differs"));
    fix.sha = sha.clone();
    fs::write(writer.join("untracked"), "local").unwrap();
    assert!(reset_pushed_fix(&writer, &fix)
        .unwrap_err()
        .contains("clean"));
    assert_eq!(git(&writer, &["rev-parse", "HEAD"]), base);
    fs::remove_file(writer.join("untracked")).unwrap();
    reset_pushed_fix(&writer, &fix).unwrap();
    assert_eq!(git(&writer, &["rev-parse", "HEAD"]), sha);
    git(&writer, &["commit", "--allow-empty", "-m", "local only"]);
    let local = git(&writer, &["rev-parse", "HEAD"]);
    assert!(reset_pushed_fix(&writer, &fix)
        .unwrap_err()
        .contains("local HEAD contains"));
    assert_eq!(git(&writer, &["rev-parse", "HEAD"]), local);
}
