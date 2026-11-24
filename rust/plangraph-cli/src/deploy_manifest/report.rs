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
    /// The user unit directory the unit files are copied into.
    pub unit_dir: PathBuf,
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
    fn installed_sha(&self, entry: &super::BinaryEntry) -> Option<String> {
        let path = match &entry.install_path {
            Some(path) => PathBuf::from(path),
            None => self
                .bin_dir
                .join(format!("{}{}", entry.bin, std::env::consts::EXE_SUFFIX)),
        };
        let meta = std::fs::metadata(&path).ok()?;
        #[cfg(unix)]
        let executable = {
            use std::os::unix::fs::PermissionsExt;
            meta.is_file() && meta.permissions().mode() & 0o111 != 0
        };
        #[cfg(not(unix))]
        let executable = meta.is_file();
        executable
            .then(|| super::stamp::sha256_file(&path))
            .flatten()
    }

    fn artifact_record(&self, bin: &str) -> Option<(String, String)> {
        super::stamp::read_artifact(&self.state_dir, bin)
    }

    fn unit_file_changed_at(&self, entry: &super::UnitEntry, file: &str) -> Option<i64> {
        if entry.scope == super::Scope::System {
            return None;
        }
        let modified = std::fs::metadata(self.unit_dir.join(file))
            .ok()?
            .modified()
            .ok()?;
        modified
            .duration_since(UNIX_EPOCH)
            .ok()
            .map(|d| d.as_secs() as i64)
    }

    fn source_tree(&self) -> Option<String> {
        super::stamp::release_stamp(&self.repo)
            .ok()
            .map(|(stamp, _)| stamp)
            .filter(|t| !t.is_empty())
    }

    fn merged_at(&self, paths: &[String]) -> Option<i64> {
        // The landing on the checkout's default line, not the topic commit: --first-parent dates a merged
        // branch by its merge, so work written days earlier does not look old the day it landed.
        let mut args = vec!["log", "-1", "--first-parent", "--format=%ct", "--"];
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
    status_full(repo, roles, sysctl, facts, ledger_path).map(|(lines, _)| lines)
}

/// [`status`] that also returns the ledger it wrote.
pub fn status_full(
    repo: &Path,
    roles: &BTreeSet<String>,
    sysctl: &dyn Systemctl,
    facts: &dyn HostFacts,
    ledger_path: Option<&Path>,
) -> Result<(Vec<String>, Ledger), String> {
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
    Ok((lines(&entries, &next, now), next))
}

fn xdg(home: &Path, key: &str, rel: &str) -> PathBuf {
    std::env::var_os(key)
        .filter(|v| !v.is_empty())
        .map_or_else(|| home.join(rel), PathBuf::from)
}

/// `$XDG_STATE_HOME/plangraph`, where the release stamps and the ledgers live.
pub fn state_dir(home: &Path) -> PathBuf {
    xdg(home, "XDG_STATE_HOME", ".local/state").join("plangraph")
}

/// One line for a digest: how many entries are not live on the hosts that reported, and the oldest.
/// Read from the ledgers only (no systemd), so a digest never blocks on a host. With the hosts registry
/// it uses the guard's rules: a host is its id or an alias (counted once, newest file), and a host with no
/// ledger or one older than the freshness limit is named, so a missing or stale host never reads as zero.
pub fn digest_line(ledger_dir: &Path, registry: Option<&str>, now: i64) -> String {
    use super::guard::{expected_hosts, ledger_for, LEDGER_MAX_AGE_HOURS};
    let (ledgers, broken) = super::guard::read_ledgers(ledger_dir);
    let hosts = registry
        .and_then(|text| expected_hosts(text).ok())
        .filter(|hosts| !hosts.is_empty());
    let (mut counted, mut missing, mut stale): (Vec<(String, &Ledger)>, Vec<String>, Vec<String>) =
        (Vec::new(), Vec::new(), Vec::new());
    match &hosts {
        Some(hosts) => {
            for host in hosts {
                match ledger_for(&ledgers, host) {
                    None => missing.push(host.id.clone()),
                    Some(l) if now - l.written_at > LEDGER_MAX_AGE_HOURS * 3600 => {
                        stale.push(host.id.clone());
                    }
                    Some(l) => counted.push((host.id.clone(), l)),
                }
            }
        }
        None => counted = ledgers.iter().map(|(h, l)| (h.clone(), l)).collect(),
    }
    if counted.is_empty() && missing.is_empty() && stale.is_empty() {
        return "deploy-not-live unknown: no host has written a deploy-status ledger".into();
    }
    let mut count = 0;
    let mut oldest: Option<(i64, String)> = None;
    for (host, ledger) in &counted {
        for (key, since) in &ledger.not_live_since {
            count += 1;
            if oldest.as_ref().is_none_or(|(first, _)| since < first) {
                oldest = Some((*since, format!("{host}:{key}")));
            }
        }
    }
    let incomplete = !missing.is_empty() || !stale.is_empty();
    let mut line = if incomplete {
        format!("deploy-not-live unknown: {count} counted")
    } else {
        format!("deploy-not-live {count}")
    };
    if let Some((since, what)) = oldest {
        line.push_str(&format!(
            " oldest {what} {}h",
            ledger::age_hours(since, now)
        ));
    }
    line.push_str(&format!(" ({} hosts reporting", counted.len()));
    for (label, names) in [("missing", &missing), ("stale", &stale)] {
        if !names.is_empty() {
            line.push_str(&format!(", {label}: {}", names.join(" ")));
        }
    }
    if hosts.is_none() {
        line.push_str(", no hosts registry: every ledger file counted");
    }
    if !broken.is_empty() {
        line.push_str(&format!(", {} unreadable", broken.len()));
    }
    line.push(')');
    line
}

/// [`digest_line`] for this host's ledger directory and hosts registry.
pub fn digest_line_local() -> String {
    let home = std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_default();
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_secs() as i64);
    digest_line(
        &ledger_dir(&state_dir(&home)),
        super::guard::registry_text(None).as_deref(),
        now,
    )
}

/// The directory of ledgers, one file per host, under the state directory.
pub fn ledger_dir(state_dir: &Path) -> PathBuf {
    state_dir.join("deploy-live")
}

pub fn host_id() -> String {
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
            "--summary" => return Ok((vec![digest_line_local()], 0)),
            other => {
                return Err(format!(
                "usage: plangraph deploy-status [--repo PATH] [--no-ledger | --summary] ({other})"
            ))
            }
        }
    }
    let state_dir = state_dir(home);
    let config = xdg(home, "XDG_CONFIG_HOME", ".config").join("plangraph");
    let roles = this_host_roles(&config)?;
    let facts = LocalFacts {
        repo: repo.clone(),
        bin_dir: home.join(".local/bin"),
        state_dir: state_dir.clone(),
        unit_dir: xdg(home, "XDG_CONFIG_HOME", ".config").join("systemd/user"),
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
