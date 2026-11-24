use super::{output, replay, ReplayReceipt, Result};
use pg_worker_core::{git::git, worktree};
use std::{fs, path::Path, process::Command};

/// Run committed checks in a disposable checkout and publish the stamp only after cleanup.
pub(super) fn run(root: &Path, base: &str) -> Result<()> {
    let base = replay::resolve(root, base)?;
    let head = replay::resolve(root, "HEAD")?;
    let stamp = worktree::fast_check_stamp(root, &head).map_err(|error| error.to_string())?;
    let state = stamp.parent().ok_or("stamp directory missing")?;
    fs::create_dir_all(state).map_err(|error| error.to_string())?;
    // Serialize receipt invalidation and publication for parallel checks of the same head.
    let lock = fs::OpenOptions::new()
        .create(true)
        .truncate(false)
        .write(true)
        .open(stamp.with_extension("lock"))
        .map_err(|e| e.to_string())?;
    lock.lock().map_err(|e| e.to_string())?;
    if let Err(error) = fs::remove_file(&stamp) {
        if error.kind() != std::io::ErrorKind::NotFound {
            return Err(error.to_string());
        }
    }
    let evidence = tempfile::tempdir_in(state).map_err(|e| e.to_string())?;
    let receipt_path = evidence.path().join("receipt.json");
    let local_vars =
        git(root, &["rev-parse", "--local-env-vars"]).map_err(|error| error.to_string())?;
    let executable = std::env::current_exe().map_err(|error| error.to_string())?;
    let directory = tempfile::Builder::new()
        .prefix("check-")
        .suffix("-fast")
        .tempdir_in(state)
        .map_err(|error| error.to_string())?;
    let checkout = directory.path().to_str().ok_or("non-UTF8 checkout path")?;
    {
        let _lock = worktree::mutation_lock(root).map_err(|error| error.to_string())?;
        git(
            root,
            &["worktree", "add", "--detach", "--", checkout, &head],
        )
        .map_err(|error| error.to_string())?;
    }
    let mut check = Command::new(executable);
    check
        .current_dir(checkout)
        .args(["fast-checks", "--base", &base])
        .env("PLANGRAPH_FAST_CHECK_RECEIPT", &receipt_path);
    for variable in local_vars.lines() {
        check.env_remove(variable);
    }
    let checked = check.status();
    let removed = (|| {
        let _lock = worktree::mutation_lock(root)?;
        git(root, &["worktree", "remove", "--force", "--", checkout])
    })();
    if let Err(error) = removed {
        // Keep the path registered for disk hygiene if Git could not remove it.
        let _ = directory.keep();
        return Err(format!("owned worktree cleanup: {error}"));
    }
    let status = checked.map_err(|error| error.to_string())?;
    if !status.success() {
        return Err(format!("owned worktree checks failed ({status})"));
    }
    let receipt = ReplayReceipt::read(&receipt_path)?;
    if !receipt.matches(&base, &head) {
        return Err("owned worktree replay receipt does not match the checked range".into());
    }
    receipt.write(&stamp)
}

/// Include local edits for interactive validation; an owned checkout has no such edits.
pub(super) fn diff_paths(root: &Path, base: &str, head: &str) -> Result<Vec<String>> {
    let merge_base = output(
        root,
        &["git".into(), "merge-base".into(), base.into(), head.into()],
    )?;
    let tracked = output(
        root,
        &[
            "git".into(),
            "diff".into(),
            "--name-only".into(),
            "--no-renames".into(),
            "-z".into(),
            merge_base.trim().into(),
        ],
    )?;
    let untracked = output(
        root,
        &[
            "git".into(),
            "ls-files".into(),
            "--others".into(),
            "--exclude-standard".into(),
            "-z".into(),
        ],
    )?;
    Ok(tracked
        .split('\0')
        .chain(untracked.split('\0'))
        .filter(|s| !s.is_empty())
        .map(String::from)
        .collect())
}
