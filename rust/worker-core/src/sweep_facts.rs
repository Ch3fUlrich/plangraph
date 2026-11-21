//! Live protection readers. Missing authority protects every worktree.
use crate::{
    disk_hygiene::{git, Guards},
    disk_runtime, host_command,
};
use pg_reap::{Facts, Worktree};
use serde::Deserialize;
use std::{
    collections::BTreeSet,
    fs, io,
    path::{Path, PathBuf},
    process::Command,
    time::{Duration, SystemTime},
};

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Claims {
    pub paths: Vec<PathBuf>,
    pub branches: Vec<String>,
}
#[derive(Deserialize)]
pub struct Pull {
    pub number: u64,
    pub state: String,
    #[serde(rename = "headRefOid")]
    pub sha: String,
}
pub use crate::unit_refs::{
    missing_worktree_refs, systemd_user_dir, unit_file_refs, unit_names, UnitRef,
};
pub struct Protection {
    pub guards: Guards,
    pub claims: Claims,
    pub units: Vec<PathBuf>,
    /// Paths named in the user unit files and drop-ins (ExecStart, Environment, ...).
    pub unit_refs: Vec<UnitRef>,
    pub cwds: Vec<PathBuf>,
}
fn output(command: &mut Command) -> io::Result<String> {
    String::from_utf8(host_command::output(command, Duration::from_secs(30))?)
        .map_err(io::Error::other)
}
pub fn unit_paths() -> io::Result<Vec<PathBuf>> {
    let mut units = BTreeSet::new();
    for verb in ["list-units", "list-unit-files"] {
        let text = output(Command::new("systemctl").args([
            "--user",
            verb,
            "--type=service",
            "--all",
            "--plain",
            "--no-legend",
            "--no-pager",
        ]))?;
        for line in text.lines() {
            let name = line
                .split_whitespace()
                .next()
                .ok_or_else(|| io::Error::other("invalid unit listing"))?;
            if !name.ends_with(".service") || name.starts_with('-') {
                return Err(io::Error::other("invalid unit name"));
            }
            // Uninstantiated templates cannot have a concrete working directory.
            if !name.ends_with("@.service") {
                units.insert(name.to_string());
            }
        }
    }
    let mut paths = Vec::new();
    if !units.is_empty() {
        let mut command = Command::new("systemctl");
        command.args(["--user", "show", "--property=WorkingDirectory", "--value"]);
        command.args(&units);
        let values = output(&mut command)?;
        for value in values.lines().filter(|s| !s.is_empty()) {
            paths.push(unit_path(value)?);
        }
    }
    Ok(paths)
}
fn unit_path(value: &str) -> io::Result<PathBuf> {
    // systemctl prefixes missing-ok paths with '-' and home-relative paths
    // expanded to an absolute directory with '!'. Both flags can be present.
    let value = value.trim_start_matches(['-', '!']);
    let path = PathBuf::from(value);
    if !path.is_absolute() || value.contains('\\') {
        return Err(io::Error::other("unknown unit WorkingDirectory"));
    }
    let canonical = path.canonicalize().unwrap_or_else(|_| path.clone());
    if canonical.components().any(|c| {
        matches!(
            c,
            std::path::Component::ParentDir | std::path::Component::CurDir
        )
    }) {
        return Err(io::Error::other("unresolved unit path"));
    }
    Ok(canonical)
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;

    #[test]
    fn working_directory_flags_preserve_protection_paths() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().canonicalize().unwrap();
        for prefix in ["", "-", "!", "-!"] {
            assert_eq!(
                unit_path(&format!("{prefix}{}", path.display())).unwrap(),
                path
            );
        }
        let missing = path.join("missing");
        assert_eq!(
            unit_path(&format!("-!{}", missing.display())).unwrap(),
            missing
        );
        for invalid in ["", "-!", "!relative", "-relative", "/bad\\x20path"] {
            assert!(unit_path(invalid).is_err(), "{invalid}");
        }
    }
}
impl Protection {
    pub fn load(pins: &Path, state: &Path, reader: Option<&Path>) -> io::Result<Self> {
        let reader =
            reader.ok_or_else(|| io::Error::other("claims reader unavailable: protected"))?;
        let guards = Guards::load(pins, state)?;
        let cwds = disk_runtime::processes()?.0;
        // Live process cwds are matched below only when inside the candidate.
        let units = unit_paths()?;
        let home = std::env::var_os("HOME").ok_or_else(|| io::Error::other("HOME unset"))?;
        let unit_refs = unit_file_refs(&systemd_user_dir()?, Path::new(&home))?;
        let claims: Claims = serde_json::from_str(&output(
            Command::new("sh")
                .env("PG_SWEEP_STATE", state)
                .args(["-c", &reader.to_string_lossy()]),
        )?)
        .map_err(io::Error::other)?;
        if claims.paths.iter().any(|p| !p.is_absolute()) {
            return Err(io::Error::other("claims paths must be absolute"));
        }
        Ok(Self {
            guards,
            claims,
            units,
            unit_refs,
            cwds,
        })
    }
}
pub fn pulls(repo: &Path, branch: &str) -> io::Result<Vec<Pull>> {
    // Detached HEADs use an exhaustive open-PR inventory, never search indexing.
    let branch_name = branch.strip_prefix("refs/heads/");
    let mut command = Command::new("gh");
    command.current_dir(repo).args([
        "pr",
        "list",
        "--state",
        if branch_name.is_some() { "all" } else { "open" },
    ]);
    if let Some(name) = branch_name {
        command.args(["--head", name]);
    }
    command.args(["--limit", "1000", "--json", "number,state,headRefOid"]);
    let text = output(&mut command)?;
    let mut rows: Vec<Pull> = serde_json::from_str(&text).map_err(io::Error::other)?;
    if rows.len() >= 1000
        || rows.iter().any(|r| {
            !["OPEN", "CLOSED", "MERGED"].contains(&r.state.as_str())
                || r.sha.len() != 40
                || !r.sha.bytes().all(|b| b.is_ascii_hexdigit())
        })
    {
        return Err(io::Error::other("incomplete PR inventory"));
    }
    if branch_name.is_none() {
        rows.retain(|row| row.sha == branch);
    }
    Ok(rows)
}
pub fn facts(
    repo: &Path,
    tree: &Worktree,
    protection: &Protection,
    prs: &[Pull],
    now: SystemTime,
) -> io::Result<Facts> {
    let path = Path::new(&tree.path);
    let branch = tree.branch.as_deref().unwrap_or("");
    let meta = fs::symlink_metadata(path)?;
    let canonical = path.canonicalize()?;
    // rev-list covers all reachable commits, including nested batch merge parents.
    let merged = git(
        repo,
        &[
            "rev-list",
            "--count",
            &tree.head,
            "--not",
            "refs/remotes/origin/main",
        ],
    )?;
    let pushed = git(
        repo,
        &[
            "for-each-ref",
            "--format=%(refname)",
            "--contains",
            &tree.head,
            "refs/remotes/origin/",
        ],
    )?;
    let overlaps = |p: &Path| {
        let p = p.canonicalize().unwrap_or_else(|_| p.to_path_buf());
        canonical.starts_with(&p) || p.starts_with(&canonical)
    };
    Ok(Facts {
        idle_hours: idle_minutes(tree, now)? / 60,
        clean: git(
            path,
            &[
                "--no-optional-locks",
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--ignored",
            ],
        )?
        .trim()
        .is_empty(),
        merged_into_main: merged.trim() == "0",
        pushed: !pushed.trim().is_empty(),
        failed: false,
        has_claim: protection.claims.paths.iter().any(|p| overlaps(p))
            || protection
                .claims
                .branches
                .iter()
                .any(|b| b == branch || branch.strip_prefix("refs/heads/") == Some(b.as_str())),
        live_run: protection.guards.protects(&canonical)
            || protection.guards.protects(path)
            || tree
                .branch
                .as_ref()
                .is_some_and(|b| Guards::protected_name(Path::new(b))),
        open_pr: prs
            .iter()
            .any(|p| p.state == "OPEN" && (tree.branch.is_some() || p.sha == tree.head)),
        in_use_by_process: protection.cwds.iter().any(|p| p.starts_with(&canonical)),
        in_use_by_unit: protection.units.iter().any(|p| p.starts_with(&canonical))
            || !unit_names(&protection.unit_refs, path, &canonical).is_empty(),
        is_clone_path: meta.file_type().is_symlink()
            || canonical == repo.canonicalize()?
            || branch == "refs/heads/main"
            || branch == "refs/heads/master",
    })
}

/// Age uses the newest HEAD/index mtime, including detached HEADs.
pub fn idle_minutes(tree: &Worktree, now: SystemTime) -> io::Result<u64> {
    let path = Path::new(&tree.path);
    let gitdir = PathBuf::from(git(path, &["rev-parse", "--absolute-git-dir"])?.trim());
    let mut newest = fs::metadata(gitdir.join("HEAD"))?.modified()?;
    match fs::metadata(gitdir.join("index")) {
        Ok(meta) => newest = newest.max(meta.modified()?),
        Err(e) if e.kind() == io::ErrorKind::NotFound => {}
        Err(e) => return Err(e),
    }
    Ok(now.duration_since(newest).unwrap_or_default().as_secs() / 60)
}

/// Run records are checked fresh for both native and legacy layouts.
pub fn terminal_run(state: &Path, path: &Path) -> io::Result<()> {
    crate::sweep_runs::check(state, path)
}
