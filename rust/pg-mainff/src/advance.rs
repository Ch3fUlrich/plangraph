//! Serialize updates and publish fixed public status for every Git refusal.
use crate::{
    imports, probe,
    process::{Output, Runner},
};
use std::{
    fs::{self, OpenOptions},
    io::Write,
    path::{Path, PathBuf},
};

pub fn status_path(run: &impl Runner, repo: &Path) -> Result<PathBuf, String> {
    Ok(PathBuf::from(imports::git(
        run,
        repo,
        &["rev-parse", "--absolute-git-dir"],
    )?)
    .join("plangraph-main-ff.status"))
}
fn checked(
    run: &impl Runner,
    repo: &Path,
    args: &[&str],
    allowed: &[i32],
) -> Result<Output, String> {
    let mut command = vec![
        "git",
        "-C",
        repo.to_str().ok_or("REFUSED repository path unavailable")?,
    ];
    command.extend(args);
    run.run(&command)
        .ok()
        .filter(|r| allowed.contains(&r.code))
        .ok_or_else(|| format!("REFUSED git {} failed", args[0]))
}
fn keeper_hold(
    run: &impl Runner,
    repo: &Path,
    state: &Path,
    target: &str,
    unit: &str,
) -> Option<String> {
    let loaded = match probe::running(state, run, cfg!(windows)) {
        Ok(loaded) => loaded,
        Err(error) if error.starts_with("keeper process query") => {
            return Some(format!("HELD {error} target {target}"))
        }
        Err(_) => {
            return Some(format!(
                "HELD keeper state/import analysis unavailable target {target}"
            ))
        }
    };
    if loaded.is_none() {
        return match probe::main_pid(run, unit) {
            Ok(0) => None,
            Ok(_) => Some(format!("HELD live keeper has no matching state; restart keeper to bootstrap and verify state path target {target}")),
            Err(error) => Some(format!("HELD {error} target {target}")),
        };
    }
    let loaded = loaded?;
    match imports::changes(run, repo, &loaded, target) {
        Ok(paths) if paths.is_empty() => None,
        Ok(paths) => Some(format!(
            "HELD keeper code changed: {} loaded {loaded} target {target}",
            paths.join(", ").replace(['\n', '\r'], "?")
        )),
        Err(_) => Some(format!(
            "HELD keeper state/import analysis unavailable target {target}"
        )),
    }
}
fn update(
    run: &impl Runner,
    repo: &Path,
    state: &Path,
    fetch: bool,
    unit: &str,
) -> Result<(i32, String), String> {
    let branch = checked(
        run,
        repo,
        &["symbolic-ref", "--short", "-q", "HEAD"],
        &[0, 1],
    )?
    .out
    .trim()
    .to_owned();
    if branch != "main" {
        return Ok((
            2,
            format!(
                "REFUSED branch {}, want main",
                if branch.is_empty() { "HEAD" } else { &branch }
            ),
        ));
    }
    if !imports::git(
        run,
        repo,
        &["status", "--porcelain", "--untracked-files=no"],
    )?
    .is_empty()
    {
        return Ok((2, "REFUSED tracked checkout changes".into()));
    }
    if fetch {
        imports::git(
            run,
            repo,
            &[
                "fetch",
                "origin",
                "+refs/heads/main:refs/remotes/origin/main",
            ],
        )?;
    }
    let target = imports::git(
        run,
        repo,
        &["rev-parse", "--verify", "origin/main^{commit}"],
    )?;
    let current = imports::git(run, repo, &["rev-parse", "--verify", "HEAD^{commit}"])?;
    let ancestry = checked(
        run,
        repo,
        &["merge-base", "--is-ancestor", &current, &target],
        &[0, 1],
    )?;
    if ancestry.code == 1 {
        return Ok((2, "REFUSED main is not an ancestor of origin/main".into()));
    }
    if let Some(line) = keeper_hold(run, repo, state, &target, unit) {
        return Ok((3, line));
    }
    if current != target {
        // A keeper may publish fresh state while the first absence probe runs.
        if let Some(line) = keeper_hold(run, repo, state, &target, unit) {
            return Ok((3, line));
        }
        imports::git(run, repo, &["merge", "--ff-only", &target])?;
    }
    Ok((
        0,
        format!(
            "{} main {target}",
            if current != target {
                "UPDATED"
            } else {
                "CURRENT"
            }
        ),
    ))
}
fn private_status(path: &Path, line: &str) -> std::io::Result<()> {
    let mut temporary = tempfile::NamedTempFile::new_in(path.parent().unwrap())?;
    writeln!(temporary, "{line}")?;
    temporary.as_file().sync_all()?;
    temporary.persist(path).map_err(|e| e.error)?;
    Ok(())
}
pub fn main_ff(
    run: &impl Runner,
    repo: &Path,
    state: &Path,
    fetch: bool,
    unit: &str,
) -> (i32, String) {
    let repo = match fs::canonicalize(repo) {
        Ok(repo) => repo,
        Err(_) => return (2, "REFUSED git rev-parse failed".into()),
    };
    let status = match status_path(run, &repo) {
        Ok(path) => path,
        Err(error) => return (2, error),
    };
    let mut options = OpenOptions::new();
    options.create(true).append(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let lock = match options.open(status.with_extension("lock")) {
        Ok(file) => file,
        Err(_) => return (2, "REFUSED status lock unavailable".into()),
    };
    if lock.lock().is_err() {
        return (2, "REFUSED status lock unavailable".into());
    }
    let result = update(run, &repo, state, fetch, unit).unwrap_or_else(|error| (2, error));
    if private_status(&status, &result.1).is_err() {
        return (2, "REFUSED status write failed".into());
    }
    result
}
