//! Attempt lifecycle decisions. Live PR/claim facts must be collected immediately before use.
use serde::Deserialize;
#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Outcome {
    Running,
    Merged,
    Succeeded,
    Failed,
    Cancelled,
}
#[derive(Debug, PartialEq, Eq)]
pub enum Action {
    Retain,
    PushAndRetain,
    BackupRequired,
    RemoveAndDeleteBranch,
    RemoveAndKeepBranch,
}
pub fn decide(
    outcome: Outcome,
    age_seconds: u64,
    open_pr: bool,
    live_claim: bool,
    dirty: bool,
) -> Action {
    if outcome == Outcome::Running {
        return Action::Retain;
    }
    if dirty {
        return Action::BackupRequired;
    }
    if outcome == Outcome::Succeeded {
        return Action::PushAndRetain;
    }
    if open_pr || live_claim || (outcome != Outcome::Merged && age_seconds < 14 * 86400) {
        Action::RemoveAndKeepBranch
    } else {
        Action::RemoveAndDeleteBranch
    }
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Request {
    pub repo: std::path::PathBuf,
    pub root: std::path::PathBuf,
    pub path: std::path::PathBuf,
    pub branch: String,
    pub outcome: Outcome,
    pub age_seconds: u64,
    pub open_pr: bool,
    pub live_claim: bool,
}

#[cfg(target_os = "linux")]
pub fn execute(request: &Request) -> std::io::Result<Action> {
    use std::{fs, io, os::fd::AsRawFd, process::Command};
    let refuse = || io::Error::other("worktree_cleanup_refused");
    let root = request.root.canonicalize()?;
    let path = match request.path.canonicalize() {
        Ok(path) => path,
        Err(e) if e.kind() == io::ErrorKind::NotFound => {
            let parent = request.path.parent().ok_or_else(refuse)?.canonicalize()?;
            parent.join(request.path.file_name().ok_or_else(refuse)?)
        }
        Err(e) => return Err(e),
    };
    if path.parent() != Some(root.as_path())
        || !path
            .file_name()
            .unwrap()
            .to_string_lossy()
            .starts_with("attempt-")
        || !request.branch.starts_with("lane/")
        || request.branch.contains(['\n', '\r'])
    {
        return Err(refuse());
    }
    let lock = fs::OpenOptions::new()
        .create(true)
        .truncate(false)
        .write(true)
        .open(root.join(".lock"))?;
    if unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
        return Err(io::Error::last_os_error());
    }
    let git = |args: &[&str]| -> io::Result<Vec<u8>> {
        let mut command = Command::new("git");
        for (key, _) in std::env::vars_os() {
            if key
                .to_string_lossy()
                .to_ascii_uppercase()
                .starts_with("GIT_")
            {
                command.env_remove(key);
            }
        }
        let output = command
            .current_dir(&request.repo)
            .env("GIT_TERMINAL_PROMPT", "0")
            .args(args)
            .output()?;
        if !output.status.success() {
            return Err(refuse());
        }
        Ok(output.stdout)
    };
    git(&[
        "check-ref-format",
        &format!("refs/heads/{}", request.branch),
    ])?;
    let inventory =
        String::from_utf8(git(&["worktree", "list", "--porcelain"])?).map_err(|_| refuse())?;
    let trees = crate::parse_porcelain(&inventory).map_err(|_| refuse())?;
    let branch_ref = format!("refs/heads/{}", request.branch);
    let head = String::from_utf8(git(&["rev-parse", "--verify", &branch_ref])?)
        .map_err(|_| refuse())?
        .trim()
        .to_owned();
    let exists = path.exists();
    if exists {
        let target = trees
            .iter()
            .skip(1)
            .find(|tree| std::path::Path::new(&tree.path) == path)
            .ok_or_else(refuse)?;
        if target.protection.is_some()
            || target.branch.as_deref() != Some(branch_ref.as_str())
            || target.head != head
            || trees
                .iter()
                .filter(|tree| tree.branch.as_deref() == Some(branch_ref.as_str()))
                .count()
                != 1
        {
            return Err(refuse());
        }
    } else if trees.iter().any(|tree| {
        tree.branch.as_deref() == Some(branch_ref.as_str())
            || std::path::Path::new(&tree.path) == path
    }) {
        return Err(refuse());
    }
    if request.outcome == Outcome::Merged {
        git(&[
            "merge-base",
            "--is-ancestor",
            &head,
            "refs/remotes/origin/main",
        ])?;
    }
    let path_text = path.to_str().ok_or_else(refuse)?;
    let dirty = exists
        && !git(&[
            "-C",
            path_text,
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--ignored",
        ])?
        .is_empty();
    let action = decide(
        request.outcome,
        request.age_seconds,
        request.open_pr,
        request.live_claim,
        dirty,
    );
    match action {
        Action::Retain => return Ok(action),
        Action::BackupRequired => {
            let backup = root.join("backups");
            fs::create_dir_all(&backup)?;
            let destination = backup.join(path.file_name().unwrap());
            fs::create_dir(&destination)?; // Existing or partial backups are never silently reused.
            let patch = git(&["-C", path_text, "diff", "--binary", "HEAD"])?;
            fs::write(destination.join("tracked.patch"), &patch)?;
            if fs::read(destination.join("tracked.patch"))? != patch {
                return Err(refuse());
            }
            let archive = destination.join("worktree.tar");
            // Full archive also preserves ignored files, modes and symlinks; .git is a registration, not work.
            let tar = |mode: &str| -> io::Result<()> {
                let status = Command::new("tar")
                    .args([mode, "--file"])
                    .arg(&archive)
                    .args(["--exclude=./.git", "--directory"])
                    .arg(&path)
                    .arg(".")
                    .status()?;
                if status.success() {
                    Ok(())
                } else {
                    Err(refuse())
                }
            };
            tar("--create")?;
            tar("--compare")?;
            fs::File::open(&archive)?.sync_all()?;
            fs::File::open(destination.join("tracked.patch"))?.sync_all()?;
            fs::File::open(&destination)?.sync_all()?;
        }
        Action::RemoveAndDeleteBranch | Action::RemoveAndKeepBranch | Action::PushAndRetain => {}
    }
    let action = decide(
        request.outcome,
        request.age_seconds,
        request.open_pr,
        request.live_claim,
        false,
    );
    if action == Action::PushAndRetain {
        git(&["push", "origin", &format!("{branch_ref}:{branch_ref}")])?;
    }
    // Compare again after backup/push; independently moved branch tips refuse deletion.
    let current = String::from_utf8(git(&["rev-parse", &branch_ref])?).map_err(|_| refuse())?;
    if current.trim() != head {
        return Err(refuse());
    }
    if exists {
        git(&["worktree", "remove", "--force", path_text])?;
    }
    if action == Action::RemoveAndDeleteBranch {
        git(&["update-ref", "-d", &branch_ref, &head])?;
    }
    Ok(action)
}
