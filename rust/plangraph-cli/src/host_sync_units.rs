//! `host-sync --install-units REPO [--scope user|system]`: install and enable the unit families the
//! deploy manifest assigns to this host's roles. The work is in `deploy_manifest::install`.
use super::Env;
use crate::deploy_manifest::install::{install_units, Target};
use crate::deploy_manifest::systemd::HostSystemctl;
use crate::deploy_manifest::{this_host_roles, Manifest, Scope};
use std::ffi::OsString;
use std::io;
use std::path::{Path, PathBuf};

const USAGE: &str = "usage: host-sync --install-units REPO [--scope user|system]";

fn parse(args: &[OsString]) -> Result<(PathBuf, Scope), String> {
    match args {
        [repo] => Ok((repo.into(), Scope::User)),
        [repo, flag, value] if flag == "--scope" && value == "user" => {
            Ok((repo.into(), Scope::User))
        }
        [repo, flag, value] if flag == "--scope" && value == "system" => {
            Ok((repo.into(), Scope::System))
        }
        _ => Err(USAGE.into()),
    }
}

fn install(
    repo: &Path,
    scope: Scope,
    home: &Path,
    config_home: Option<PathBuf>,
) -> io::Result<bool> {
    let repo = repo.canonicalize()?;
    let manifest = Manifest::load_from_repo(&repo).map_err(io::Error::other)?;
    let config = config_home.unwrap_or_else(|| home.join(".config"));
    let roles = this_host_roles(&config.join("plangraph")).map_err(io::Error::other)?;
    let (unit_dir, config_dir) = match scope {
        Scope::User => (config.join("systemd/user"), config.join("plangraph")),
        Scope::System => (
            PathBuf::from("/etc/systemd/system"),
            PathBuf::from("/etc/plangraph"),
        ),
    };
    let target = Target {
        repo: &repo,
        unit_dir: &unit_dir,
        config_dir: &config_dir,
        scope,
    };
    let report = install_units(&manifest, &roles, &target, &HostSystemctl);
    report.lines.iter().for_each(|line| println!("{line}"));
    Ok(report.pending)
}

pub(super) fn run(args: &[OsString]) -> i32 {
    let (repo, scope) = match parse(args) {
        Ok(parsed) => parsed,
        Err(usage) => {
            eprintln!("{usage}");
            return 2;
        }
    };
    let env = Env::from_process();
    let result = env
        .home
        .ok_or_else(|| io::Error::other("HOME is not set"))
        .and_then(|home| install(&repo, scope, &home, env.xdg_config_home));
    match result {
        Ok(false) => 0,
        Ok(true) => {
            eprintln!("FAIL install units: some families are pending (see the lines above)");
            1
        }
        Err(error) => {
            eprintln!("FAIL install units: {error}");
            1
        }
    }
}
