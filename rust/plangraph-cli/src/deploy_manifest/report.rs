//! `plangraph deploy-status`: every manifest entry with its live state on this host, how long each
//! not-live entry has been so, and the count. Writes the ledger the GO check reads.
use super::ledger::{self, Ledger};
use super::live::{read_live, Entry, HostFacts};
use super::systemd::{HostSystemctl, Systemctl};
use super::{this_host_roles, Manifest};
use std::collections::BTreeSet;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{SystemTime, UNIX_EPOCH};

/// This host as the status run reads it: the checkout, the bin directory, the release stamps.
pub struct LocalFacts {
    pub repo: PathBuf,
    pub bin_dir: PathBuf,
    pub state_dir: PathBuf,
}

fn git(repo: &Path, args: &[&str]) -> Option<String> {
    let mut command = Command::new("git");
    pg_worker_core::host_command::isolate_git(&mut command);
    let out = command.current_dir(repo).args(args).output().ok()?;
    out.status
        .success()
        .then(|| String::from_utf8_lossy(&out.stdout).trim().to_owned())
}

impl HostFacts for LocalFacts {
    fn binary_installed(&self, bin: &str) -> bool {
        let path = self
            .bin_dir
            .join(format!("{bin}{}", std::env::consts::EXE_SUFFIX));
        let Ok(meta) = std::fs::metadata(&path) else {
            return false;
        };
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            meta.is_file() && meta.permissions().mode() & 0o111 != 0
        }
        #[cfg(not(unix))]
        {
            meta.is_file()
        }
    }

    fn built_trees(&self) -> Vec<String> {
        ["release-built-tree", "release-keeper-built-tree"]
            .iter()
            .filter_map(|name| std::fs::read_to_string(self.state_dir.join(name)).ok())
            .map(|text| text.trim().to_owned())
            .filter(|tree| !tree.is_empty())
            .collect()
    }

    fn source_tree(&self) -> Option<String> {
        git(&self.repo, &["rev-parse", "HEAD:rust"]).filter(|t| !t.is_empty())
    }

    fn merged_at(&self, paths: &[String]) -> Option<i64> {
        let mut args = vec!["log", "-1", "--format=%ct", "--"];
        args.extend(paths.iter().map(String::as_str));
        git(&self.repo, &args)?.parse().ok()
    }

    fn now(&self) -> i64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_or(0, |d| d.as_secs() as i64)
    }
}

/// One line per entry, then the count: `deploy-entry<TAB>kind<TAB>name<TAB>state<TAB>age<TAB>first-run`.
pub fn lines(entries: &[Entry], ledger: &Ledger, now: i64) -> Vec<String> {
    let mut out: Vec<String> = entries
        .iter()
        .map(|entry| {
            let age = ledger.not_live_since.get(&entry.key()).map_or_else(
                || "-".to_owned(),
                |since| format!("{}h", ledger::age_hours(*since, now)),
            );
            let check = if entry.live.is_live() {
                String::new()
            } else {
                entry.first_run.clone()
            };
            let kind = if entry.kind == super::live::Kind::Unit {
                "unit"
            } else {
                "binary"
            };
            format!(
                "deploy-entry\t{kind}\t{}\t{}\t{age}\t{check}",
                entry.name,
                entry.live.text()
            )
        })
        .collect();
    out.push(format!("deploy-not-live\t{}", ledger::counts(entries)));
    out
}

/// Read the manifest of `repo`, the live state of this host, update the ledger at `ledger_path`.
pub fn status(
    repo: &Path,
    roles: &BTreeSet<String>,
    sysctl: &dyn Systemctl,
    facts: &dyn HostFacts,
    ledger_path: Option<&Path>,
) -> Result<Vec<String>, String> {
    let manifest = Manifest::load_from_repo(repo)?;
    let entries = read_live(&manifest, roles, sysctl, facts);
    let now = facts.now();
    let next = ledger::update(
        &ledger_path.map(ledger::load).unwrap_or_default(),
        &entries,
        now,
    );
    if let Some(path) = ledger_path {
        ledger::save(path, &next)?;
    }
    Ok(lines(&entries, &next, now))
}

/// The directory of ledgers, one file per host, under the state directory.
pub fn ledger_dir(state_dir: &Path) -> PathBuf {
    state_dir.join("deploy-live")
}

fn host_id() -> String {
    std::env::var("PLANGRAPH_HOST_NAME")
        .ok()
        .filter(|v| !v.trim().is_empty())
        .or_else(|| {
            std::fs::read_to_string("/etc/hostname")
                .ok()
                .map(|s| s.trim().to_owned())
        })
        .filter(|v| {
            v.chars()
                .all(|c| c.is_ascii_alphanumeric() || "._-".contains(c))
                && !v.is_empty()
        })
        .unwrap_or_else(|| "local".into())
}

/// `plangraph deploy-status [--repo PATH] [--no-ledger]` on this host; prints the lines and returns the
/// not-live count (the exit status is 1 while any entry is not live).
pub fn run_local(home: &Path, args: &[String]) -> Result<(Vec<String>, usize), String> {
    let (mut repo, mut write_ledger) = (home.join("code/plangraph"), true);
    let mut it = args.iter();
    while let Some(arg) = it.next() {
        match arg.as_str() {
            "--repo" => repo = it.next().ok_or("--repo needs a path")?.into(),
            "--no-ledger" => write_ledger = false,
            other => {
                return Err(format!(
                    "usage: plangraph deploy-status [--repo PATH] [--no-ledger] ({other})"
                ))
            }
        }
    }
    let xdg = |key: &str, rel: &str| {
        std::env::var_os(key)
            .filter(|v| !v.is_empty())
            .map_or_else(|| home.join(rel), PathBuf::from)
    };
    let state_dir = xdg("XDG_STATE_HOME", ".local/state").join("plangraph");
    let config = xdg("XDG_CONFIG_HOME", ".config").join("plangraph");
    let roles = this_host_roles(&config)?;
    let facts = LocalFacts {
        repo: repo.clone(),
        bin_dir: home.join(".local/bin"),
        state_dir: state_dir.clone(),
    };
    let ledger_file = ledger_dir(&state_dir).join(format!("{}.json", host_id()));
    let out = status(
        &repo,
        &roles,
        &HostSystemctl,
        &facts,
        write_ledger.then_some(ledger_file.as_path()),
    )?;
    let count = out
        .last()
        .and_then(|l| l.strip_prefix("deploy-not-live\t"))
        .and_then(|n| n.parse().ok())
        .unwrap_or(0);
    Ok((out, count))
}

/// The CLI wrapper: prints, exits 1 while any entry is not live.
pub fn run(home: &Path, args: &[String]) -> i32 {
    match run_local(home, args) {
        Ok((lines, count)) => {
            lines.iter().for_each(|l| println!("{l}"));
            i32::from(count > 0)
        }
        Err(error) => {
            eprintln!("deploy-status: {error}");
            2
        }
    }
}
