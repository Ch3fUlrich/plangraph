//! Host install dry run: what installing one unit family or one built binary needs on a host.
//!
//! `host-sync --install-dry-run [--registry FILE] [--repo DIR] HOST FAMILY` prints one
//! `MISSING <binary|file|env|path> NAME` line per absent thing and exits 1; a host that has everything
//! exits 0 with no output. Any other exit refuses the target (the deploy plan treats it as a refusal).
//!
//! The work has two steps so that one implementation serves local and ssh hosts: `plan` reads the unit
//! sources of the repository into a `Needs` record, and `evaluate` checks that record against a host's
//! files. A remote host receives the record on stdin of `--install-dry-run-here`.
use super::{is_executable, Env};
use pg_worker_core::hosts::{HostEntry, HostRegistry, Os};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};
use std::ffi::OsString;
use std::io::{Read, Write};
#[cfg(test)]
use std::path::Path;
use std::path::PathBuf;
use std::process::{Command, Stdio};

#[path = "host_sync_dry_run_eval.rs"]
mod eval;
#[path = "host_sync_dry_run_plan.rs"]
mod plan;

/// `(kind, name)`; kind is `binary`, `file`, `env` or `path`.
pub(super) type Missing = (String, String);

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub(super) struct EnvFile {
    pub path: String,
    pub optional: bool,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub(super) struct Built {
    pub name: String,
    /// The repository root on the host, when the registry names one.
    pub repo: Option<String>,
}

/// What an install needs on the host. Paths may carry `%h` (the host's home) and `%%`.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
pub(super) struct Needs {
    /// Things already known absent where the sources are read (a unit file the repository lacks).
    pub absent: BTreeSet<Missing>,
    pub binaries: BTreeSet<String>,
    pub env_files: Vec<EnvFile>,
    pub environment: BTreeMap<String, String>,
    /// Variables the `ExecStart` lines read.
    pub keys: BTreeSet<String>,
    pub dirs: BTreeSet<String>,
    /// Programs whose own settings the host must hold, as `state-wait:ROLE`.
    #[serde(default)]
    pub programs: BTreeSet<String>,
    pub built: Option<Built>,
}

use eval::evaluate;
#[cfg(test)]
use eval::shown;
use plan::plan;

fn print_result(missing: &BTreeSet<Missing>) -> i32 {
    for (kind, name) in missing {
        println!("MISSING {kind} {name}");
    }
    i32::from(!missing.is_empty())
}

fn usage() -> i32 {
    eprintln!("usage: host-sync --install-dry-run [--registry FILE] [--repo DIR] [--units DIR] HOST FAMILY | --install-dry-run-here (needs on stdin)");
    2
}

/// `--install-dry-run-here`: the needs record on stdin, checked against this host.
pub(super) fn run_here() -> i32 {
    let mut raw = String::new();
    let parsed = std::io::stdin()
        .read_to_string(&mut raw)
        .map_err(|e| e.to_string())
        .and_then(|_| serde_json::from_str::<Needs>(&raw).map_err(|e| e.to_string()));
    let (Ok(needs), Some(home)) = (parsed, Env::from_process().home) else {
        eprintln!("host-sync: unreadable needs record or HOME is not set");
        return 2;
    };
    print_result(&evaluate(&needs, &home, &|k| std::env::var(k).ok()))
}

fn run_remote(entry: &HostEntry, route: &str, needs: &Needs) -> i32 {
    if route.starts_with('-') || route.contains(char::is_whitespace) {
        eprintln!("host-sync: unusable ssh route for {}", entry.id);
        return 2;
    }
    let spawned = Command::new("ssh")
        .args([
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            "--",
            route,
        ])
        .arg(".local/bin/plangraph host-sync --install-dry-run-here")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn();
    let output = spawned.and_then(|mut child| {
        let body = serde_json::to_vec(needs).map_err(std::io::Error::other)?;
        child.stdin.take().expect("piped").write_all(&body)?;
        child.wait_with_output()
    });
    match output {
        Ok(out) if matches!(out.status.code(), Some(0 | 1)) => {
            print!("{}", String::from_utf8_lossy(&out.stdout));
            out.status.code().unwrap_or(2)
        }
        Ok(out) => {
            eprintln!("host-sync: dry run on {} failed: {}", entry.id, out.status);
            2
        }
        Err(e) => {
            eprintln!("host-sync: dry run on {} did not start: {e}", entry.id);
            2
        }
    }
}

pub(super) fn run(args: &[OsString]) -> i32 {
    let mut flags: BTreeMap<&str, PathBuf> = BTreeMap::new();
    let mut positional = Vec::new();
    let mut iter = args.iter();
    while let Some(arg) = iter.next() {
        match arg.to_str() {
            Some(flag @ ("--registry" | "--repo" | "--units")) => match iter.next() {
                Some(value) => {
                    flags.insert(flag, PathBuf::from(value));
                }
                None => return usage(),
            },
            Some(value) if !value.starts_with("--") => positional.push(value.to_owned()),
            _ => return usage(),
        }
    }
    let [host, family] = positional.as_slice() else {
        return usage();
    };
    let env = Env::from_process();
    let Some(home) = env.home.clone() else {
        eprintln!("host-sync: HOME is not set");
        return 2;
    };
    let registry_path = flags.get("--registry").cloned().unwrap_or_else(|| {
        env.xdg_config_home
            .clone()
            .unwrap_or_else(|| home.join(".config"))
            .join("plangraph/hosts.json")
    });
    let registry = match HostRegistry::load(&registry_path) {
        Ok(registry) => registry,
        Err(e) => {
            eprintln!("host-sync: cannot load the hosts registry: {e}");
            return 2;
        }
    };
    let Some(entry) = registry.resolve(host) else {
        eprintln!("host-sync: {host} is not in the hosts registry");
        return 2;
    };
    if entry.os != Os::Linux {
        eprintln!(
            "host-sync: no install dry run for the {:?} host {}",
            entry.os, entry.id
        );
        return 2;
    }
    let repo = flags
        .get("--repo")
        .cloned()
        .unwrap_or_else(|| PathBuf::from("."));
    let local = entry.id == registry.self_host().id;
    let host_repo = if local {
        repo.canonicalize().ok().map(|p| p.display().to_string())
    } else {
        entry.repo("plangraph").map(|r| r.root.clone())
    };
    let units = flags
        .get("--units")
        .cloned()
        .unwrap_or_else(|| repo.join("deploy/systemd"));
    let needs = match plan(family, &units, host_repo.as_deref()) {
        Ok(needs) => needs,
        Err(e) => {
            eprintln!("host-sync: {e}");
            return 2;
        }
    };
    if local {
        return print_result(&evaluate(&needs, &home, &|k| std::env::var(k).ok()));
    }
    match entry.ssh_route() {
        Some(route) => run_remote(entry, &route, &needs),
        None => {
            eprintln!("host-sync: no route to {}", entry.id);
            2
        }
    }
}

#[cfg(all(test, unix))]
#[path = "host_sync_dry_run_tests.rs"]
mod tests;
