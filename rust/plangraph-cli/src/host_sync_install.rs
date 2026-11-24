//! `host-sync --install [--check] [--fleet] [--repo PATH] [--hosts FILE] [--machine]`: the one command
//! that makes a host complete. It reads `deploy/manifest.toml`, prints what is missing, installs it, and
//! writes the deploy ledger; `--check` stops after the plan and exits 1 while anything is missing;
//! `--fleet` does the same on every host of the hosts registry from here. The work is in
//! `deploy_manifest::apply` and `deploy_manifest::fleet`.
use super::Env;
use crate::deploy_manifest::apply::{self, Host, Steps};
use crate::deploy_manifest::fleet::{hosts_from_registry, run_fleet, Processes};
use crate::deploy_manifest::report::{host_id, ledger_dir, state_dir, LocalFacts};
use crate::deploy_manifest::systemd::HostSystemctl;
use crate::deploy_manifest::{this_host_roles, Manifest};
use std::ffi::OsString;
use std::io;
use std::path::{Path, PathBuf};

const USAGE: &str =
    "usage: host-sync --install [--check] [--fleet] [--repo PATH] [--hosts FILE] [--machine]";

#[derive(Default)]
struct Args {
    check: bool,
    fleet: bool,
    machine: bool,
    repo: Option<PathBuf>,
    hosts: Option<PathBuf>,
}

fn parse(args: &[OsString]) -> Result<Args, String> {
    let mut out = Args::default();
    let mut it = args.iter();
    while let Some(arg) = it.next() {
        match arg.to_str() {
            Some("--check") => out.check = true,
            Some("--fleet") => out.fleet = true,
            Some("--machine") => out.machine = true,
            Some("--repo") => out.repo = Some(it.next().ok_or(USAGE)?.into()),
            Some("--hosts") => out.hosts = Some(it.next().ok_or(USAGE)?.into()),
            _ => return Err(USAGE.into()),
        }
    }
    Ok(out)
}

/// The release build and the env defaults of this host, through the existing installers.
struct RealSteps<'a> {
    repo: &'a Path,
    home: &'a Path,
}

impl Steps for RealSteps<'_> {
    fn release(&self) -> Result<(), String> {
        super::release::install(self.repo, self.home, false).map_err(|e| e.to_string())
    }

    fn env_family(&self, family: &str) -> Result<(), String> {
        super::unit_env::install(family, self.home, &[]).map_err(|e| e.to_string())
    }
}

fn config_home(env: &Env, home: &Path) -> PathBuf {
    env.xdg_config_home
        .clone()
        .unwrap_or_else(|| home.join(".config"))
}

fn install_here(args: &Args, env: &Env, home: &Path) -> io::Result<bool> {
    let repo = args
        .repo
        .clone()
        .unwrap_or_else(|| home.join("code/plangraph"))
        .canonicalize()?;
    let manifest = Manifest::load_from_repo(&repo).map_err(io::Error::other)?;
    let config = config_home(env, home);
    let roles = this_host_roles(&config.join("plangraph")).map_err(io::Error::other)?;
    let state = state_dir(home);
    let unit_dir = config.join("systemd/user");
    let config_dir = config.join("plangraph");
    let host = Host {
        repo: &repo,
        unit_dir: &unit_dir,
        config_dir: &config_dir,
        ledger_path: Some(ledger_dir(&state).join(format!("{}.json", host_id()))),
    };
    let facts = LocalFacts {
        repo: repo.clone(),
        bin_dir: home.join(".local/bin"),
        state_dir: state,
        unit_dir: unit_dir.clone(),
    };
    let steps = RealSteps { repo: &repo, home };
    let outcome = apply::run(
        &manifest,
        &roles,
        &host,
        &HostSystemctl,
        &facts,
        &steps,
        args.check,
    )
    .map_err(io::Error::other)?;
    outcome.lines.iter().for_each(|line| println!("{line}"));
    if args.machine {
        println!("pending\t{}", u8::from(outcome.pending));
        println!(
            "ledger\t{}",
            serde_json::to_string(&outcome.ledger).map_err(io::Error::other)?
        );
    }
    Ok(outcome.pending)
}

fn install_fleet(args: &Args, env: &Env, home: &Path) -> io::Result<bool> {
    let registry = args
        .hosts
        .clone()
        .unwrap_or_else(|| config_home(env, home).join("plangraph/hosts.json"));
    let text = std::fs::read_to_string(&registry)
        .map_err(|e| io::Error::other(format!("{}: {e}", registry.display())))?;
    let hosts = hosts_from_registry(&text).map_err(io::Error::other)?;
    let (table, failed) = run_fleet(
        &hosts,
        &Processes,
        args.check,
        &ledger_dir(&state_dir(home)),
    );
    table.iter().for_each(|line| println!("{line}"));
    Ok(failed)
}

pub(super) fn run(args: &[OsString]) -> i32 {
    let parsed = match parse(args) {
        Ok(parsed) => parsed,
        Err(usage) => {
            eprintln!("{usage}");
            return 2;
        }
    };
    let env = Env::from_process();
    let Some(home) = env.home.clone() else {
        eprintln!("FAIL install: HOME is not set");
        return 1;
    };
    let result = if parsed.fleet {
        install_fleet(&parsed, &env, &home)
    } else {
        install_here(&parsed, &env, &home)
    };
    match result {
        Ok(false) => 0,
        Ok(true) => 1,
        Err(error) => {
            eprintln!("FAIL install: {error}");
            1
        }
    }
}
