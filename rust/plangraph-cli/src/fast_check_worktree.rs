use super::{output, Result};
use pg_worker_core::{git::git, worktree};
use std::{fs, io::Write, path::Path, process::Command};

/// Run committed checks in a disposable checkout and publish the stamp only after cleanup.
pub(super) fn run(root: &Path, args: &[String]) -> Result<()> {
    let head = git(root, &["rev-parse", "HEAD"]).map_err(|error| error.to_string())?;
    let stamp = worktree::fast_check_stamp(root, &head).map_err(|error| error.to_string())?;
    let state = stamp.parent().ok_or("stamp directory missing")?;
    fs::create_dir_all(state).map_err(|error| error.to_string())?;
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
        .arg("fast-checks")
        .args(args.iter().filter(|arg| arg.as_str() != "--owned-worktree"));
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
    let mut receipt = tempfile::NamedTempFile::new_in(state).map_err(|error| error.to_string())?;
    writeln!(receipt, "{head}").map_err(|error| error.to_string())?;
    receipt.persist(stamp).map_err(|error| error.to_string())?;
    Ok(())
}

/// Include local edits for interactive validation; an owned checkout has no such edits.
pub(super) fn diff_paths(root: &Path, base: &str) -> Result<Vec<String>> {
    let merge_base = output(
        root,
        &[
            "git".into(),
            "merge-base".into(),
            base.into(),
            "HEAD".into(),
        ],
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
