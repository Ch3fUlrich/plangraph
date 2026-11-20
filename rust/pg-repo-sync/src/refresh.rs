//! One sweep after a main update. Hooks and the repository's merge attributes stay active.
use serde::Deserialize;
use std::{
    path::{Path, PathBuf},
    process::{Command, Stdio},
};

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct Pr {
    number: u64,
    head_ref_name: String,
    head_ref_oid: String,
    is_cross_repository: bool,
}

// drift::git deliberately disables hooks for inspection, so it cannot serve writes here.
fn git(repo: &Path, args: &[&str]) -> Command {
    let mut cmd = Command::new("git");
    for key in [
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_COMMON_DIR",
        "GIT_PREFIX",
    ] {
        cmd.env_remove(key);
    }
    cmd.arg("-C").arg(repo).args(args);
    cmd
}
fn output(mut cmd: Command) -> Result<String, String> {
    let out = cmd.output().map_err(|e| e.to_string())?;
    if !out.status.success() {
        return Err(format!(
            "{cmd:?}: {}",
            String::from_utf8_lossy(&out.stderr).trim()
        ));
    }
    String::from_utf8(out.stdout)
        .map(|s| s.trim().into())
        .map_err(|e| e.to_string())
}
fn gh(args: &[&str]) -> Command {
    let mut cmd = Command::new("gh");
    cmd.args(args);
    cmd
}
fn notify(executable: &Path, line: &str) -> Result<(), String> {
    use std::io::Write;
    println!("{line}");
    let mut child = Command::new(executable)
        .stdin(Stdio::piped())
        .spawn()
        .map_err(|e| e.to_string())?;
    let written = writeln!(child.stdin.take().ok_or("missing alert stdin")?, "{line}");
    let status = child.wait().map_err(|e| e.to_string())?;
    written.map_err(|e| e.to_string())?;
    if !status.success() {
        return Err(format!("orchestrator notification failed: {status}"));
    }
    Ok(())
}

fn refresh(repo: &Path, github: &str, pr: &Pr, main: &str) -> Result<(), String> {
    if pr.is_cross_repository || pr.head_ref_name == "main" {
        return Err("unsupported fork or main head".into());
    }
    output(git(
        repo,
        &[
            "check-ref-format",
            &format!("refs/heads/{}", pr.head_ref_name),
        ],
    ))?;
    output(git(
        repo,
        &[
            "fetch",
            "--no-tags",
            "origin",
            &format!("refs/heads/{}", pr.head_ref_name),
        ],
    ))?;
    let head = output(git(repo, &["rev-parse", "FETCH_HEAD"]))?;
    if head != pr.head_ref_oid {
        return Err("PR head moved since listing".into());
    }
    let scratch = tempfile::tempdir().map_err(|e| e.to_string())?;
    let path = scratch.path().join("checkout");
    let wt = path.to_str().ok_or("non-UTF8 scratch path")?;
    output(git(repo, &["worktree", "add", "--detach", wt, &head]))?;
    // Always unregister the worktree, including merge-hook and push-hook refusals.
    let result = (|| {
        let merged = git(&path, &["merge", "--no-edit", "--no-ff", main])
            .output()
            .map_err(|e| e.to_string())?;
        if !merged.status.success() {
            let files = output(git(&path, &["diff", "--name-only", "--diff-filter=U"]))?;
            if files.is_empty() {
                return Err(format!(
                    "merge failed: {}",
                    String::from_utf8_lossy(&merged.stderr).trim()
                ));
            }
            output(git(&path, &["merge", "--abort"]))?;
            let mut label = Command::new("gh");
            label.args([
                "api",
                "--method",
                "POST",
                &format!("repos/{github}/issues/{}/labels", pr.number),
                "--raw-field",
                "labels[]=needs-update",
            ]);
            output(label)?;
            return Err(format!(
                "conflict {}",
                files.split_whitespace().collect::<Vec<_>>().join(", ")
            ));
        }
        let refreshed = output(git(&path, &["rev-parse", "HEAD"]))?;
        if refreshed != head {
            output(git(
                &path,
                &[
                    "push",
                    "origin",
                    &format!("HEAD:refs/heads/{}", pr.head_ref_name),
                ],
            ))?;
            println!("REFRESHED #{} {refreshed}", pr.number);
        }
        Ok(())
    })();
    let removed = output(git(repo, &["worktree", "remove", "--force", wt]));
    match (result, removed) {
        (Err(error), Err(cleanup)) => Err(format!("{error}; cleanup failed: {cleanup}")),
        (Err(error), _) | (_, Err(error)) => Err(error),
        _ => Ok(()),
    }
}

pub fn run(args: impl Iterator<Item = std::ffi::OsString>) -> Result<bool, String> {
    let (mut repo, mut github, mut alert) = (None, None, None);
    let mut args = args;
    while let Some(arg) = args.next() {
        match arg.to_str() {
            Some("--repo") => repo = Some(PathBuf::from(args.next().ok_or("missing repo")?)),
            Some("--github") => github = Some(args.next().ok_or("missing GitHub repository")?.into_string().map_err(|_| "invalid GitHub repository")?),
            Some("--notify") => alert = Some(PathBuf::from(args.next().ok_or("missing notifier")?)),
            _ => return Err("usage: pg-repo-sync refresh-prs --repo PATH --github OWNER/REPO --notify EXECUTABLE".into()),
        }
    }
    let repo = repo.ok_or("missing --repo")?;
    let github = github.ok_or("missing --github")?;
    let alert = alert.ok_or("missing --notify")?;
    sweep(&repo, &github, &alert)
}

pub fn sweep(repo: &Path, github: &str, alert: &Path) -> Result<bool, String> {
    if github.split('/').count() != 2
        || github.split('/').any(|p| {
            p.is_empty()
                || !p
                    .bytes()
                    .all(|c| c.is_ascii_alphanumeric() || b"-_.".contains(&c))
        })
    {
        return Err("invalid GitHub repository".into());
    }
    output(git(
        repo,
        &[
            "fetch",
            "--no-tags",
            "origin",
            "+refs/heads/main:refs/remotes/origin/main",
        ],
    ))?;
    let main = output(git(repo, &["rev-parse", "refs/remotes/origin/main"]))?;
    let prs: Vec<Pr> = serde_json::from_str(&output(gh(&[
        "pr",
        "list",
        "--repo",
        github,
        "--state",
        "open",
        "--label",
        "batch-ready",
        "--limit",
        "100000",
        "--json",
        "number,headRefName,headRefOid,isCrossRepository",
    ]))?)
    .map_err(|e| e.to_string())?;
    let mut problems = false;
    for pr in prs {
        if let Err(error) = refresh(repo, github, &pr, &main) {
            problems = true;
            if let Err(error) = notify(alert, &format!("ALERT #{} {error}", pr.number)) {
                eprintln!("{error}");
            }
        }
    }
    Ok(problems)
}
