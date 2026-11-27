use super::{output, Result};
use pg_worker_core::{fast_check_stamp as receipt, git::git, worktree};
use std::process::ExitStatus;
use std::{fs, path::Path, process::Command};

/// The child writes one `check` line per executed step into the file this variable names.
pub(super) const RESULTS_ENV: &str = "PG_FAST_CHECK_RESULTS";

/// Read and remove the child's results; a missing file is no check.
fn read_checks(path: &Path) -> Result<(Vec<receipt::Check>, Option<receipt::BudgetOverrun>)> {
    let text = fs::read_to_string(path).unwrap_or_default();
    let _ = fs::remove_file(path);
    let checks = text
        .lines()
        .filter_map(|line| {
            let mut words = line.strip_prefix("check ")?.split_whitespace();
            let label = words.next()?.to_string();
            let rc = words.next()?.strip_prefix("rc=")?.parse().ok()?;
            let seconds = words.next()?.strip_prefix("wall=")?.parse().ok()?;
            Some(receipt::Check { label, rc, seconds })
        })
        .collect();
    Ok((checks, receipt::read_budget(&text)?))
}

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
    let results = state.join(format!("results-{}-{head}", std::process::id()));
    let mut check = Command::new(executable);
    check
        .env(RESULTS_ENV, &results)
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
    settle(root, &head, &results, status)
}

pub(super) fn settle(root: &Path, head: &str, results: &Path, status: ExitStatus) -> Result<()> {
    let (mut checks, budget) = read_checks(results)?;
    if !status.success() && checks.iter().all(|check| check.rc == 0) {
        // A child that died before it listed a failure still leaves a red receipt.
        checks.push(receipt::Check {
            label: "fast-checks".into(),
            rc: status.code().unwrap_or(-1),
            seconds: 0.0,
        });
    }
    // The receipt is written for every outcome: a red one makes the push and review refuse this head.
    receipt::publish_with_budget(root, head, &checks, budget)?;
    if !status.success() {
        return Err(format!("owned worktree checks failed ({status})"));
    }
    Ok(())
}

#[cfg(test)]
pub(super) fn fixture_status(passed: bool) -> ExitStatus {
    let arg = if passed { "--version" } else { "--bad" };
    Command::new("git").arg(arg).output().unwrap().status
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
