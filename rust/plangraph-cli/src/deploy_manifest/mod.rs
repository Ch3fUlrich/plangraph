//! The deploy manifest: the one table that names every systemd unit and every host binary the tree
//! builds, the host roles each belongs on, the env file names it reads and its first-run check.
//!
//! `deploy/manifest.toml` is read by the CI coverage test (a unit file or binary target the manifest
//! does not name fails the build). Nothing here reads systemd, so no code in this module touches a host.
//! An entry is either deployed (roles + first-run check) or `not_deployed = "<reason>"`; that is the
//! only way to leave a unit or binary out. The file names roles, never host names, and only env file
//! names, never values; a host learns its roles from `PG_HOST_ROLES` or `~/.config/plangraph/host-roles`.
use serde::Deserialize;
use std::collections::BTreeSet;
use std::path::Path;

mod consumers;
pub mod roles;
pub mod scan;
mod validate;

pub use roles::{host_roles, this_host_roles};

/// The role that every host has.
pub const ALL: &str = "all";
/// The roles a host can hold: the keeper host, a spawn worker host, a WSL worker host.
pub const ROLES: [&str; 3] = ["keeper", "worker", "wsl"];
/// Where the manifest lives in the tree.
pub const MANIFEST_PATH: &str = "deploy/manifest.toml";

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Scope {
    #[default]
    User,
    System,
}

/// A family of unit files (a service and its timer, or one service) that is installed together.
#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct UnitEntry {
    pub name: String,
    /// File names under `deploy/<dir>` (`deploy/systemd` unless `dir` says otherwise).
    pub files: Vec<String>,
    /// The directory under `deploy/` that holds the unit files (`systemd` unless named): units that ship
    /// beside their tooling (`liveness`, `pganchor`, `watchdog`) name it, and `.` is `deploy/` itself.
    pub dir: Option<String>,
    /// How a file that is a template (`*.service.in`, or a unit with `@ROOT@` placeholders) becomes an
    /// installed unit: the script in the tree that runs the step and the unit names it writes.
    pub installer: Option<Installer>,
    /// The unit that is enabled and must be active: the timer of a timer family, else the service.
    pub enable: Option<String>,
    #[serde(default)]
    pub scope: Scope,
    #[serde(default)]
    pub hosts: Vec<String>,
    /// Names of the files under the config directory the unit reads; never their values. `a|b` means
    /// either file serves.
    #[serde(default)]
    pub env_files: Vec<String>,
    /// The `host-sync --install-env` family that creates the env files from documented defaults; an env
    /// file without one is reported by name for the operator (a secret is never created).
    pub env_family: Option<String>,
    /// What proves the unit works after the first start; the status also reads the last run result.
    pub first_run: Option<String>,
    pub not_deployed: Option<String>,
}

/// The installer of a unit family whose files are templates, not units to copy verbatim.
#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Installer {
    /// A tracked file of the repository that runs the step (`deploy/bootstrap.sh`).
    pub via: String,
    /// The command the script runs, as one fixed statement (`python -m ... install --host linux`).
    pub step: String,
    /// The unit names the step writes.
    pub renders: Vec<String>,
}

impl Installer {
    fn validate(&self, what: &str) -> Result<(), String> {
        let via_ok = !self.via.starts_with('/')
            && !self.via.is_empty()
            && !self
                .via
                .split('/')
                .any(|c| c.is_empty() || c == "." || c == "..");
        if !via_ok {
            return Err(format!(
                "{what}: installer via must be a repository file path"
            ));
        }
        if self.step.trim().is_empty() || self.renders.is_empty() {
            return Err(format!(
                "{what}: installer needs a step and the unit names it renders"
            ));
        }
        if self.renders.iter().any(|r| !scan::is_unit_name(r)) {
            return Err(format!("{what}: installer renders must be unit file names"));
        }
        Ok(())
    }
}

/// One place a consumer runs a binary from, on the hosts that hold one of `hosts`.
#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct InstallPath {
    /// Absolute, or `~/` for the home directory of the user that runs the unit.
    pub path: String,
    pub hosts: Vec<String>,
}

/// One binary target of a workspace crate.
#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BinaryEntry {
    /// The Cargo package that builds it.
    pub package: String,
    pub bin: String,
    /// Every path a consumer runs the binary from, each with the host roles it applies to (a system
    /// unit that runs `/usr/local/bin/<bin>`, a user unit that runs `~/.local/bin/<bin>`). No list means
    /// only `~/.local/bin/<bin>`; a binary used from two places lists both, none is guessed.
    #[serde(default)]
    pub install_paths: Vec<InstallPath>,
    #[serde(default)]
    pub hosts: Vec<String>,
    pub first_run: Option<String>,
    pub not_deployed: Option<String>,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Manifest {
    #[serde(default)]
    pub unit: Vec<UnitEntry>,
    #[serde(default)]
    pub binary: Vec<BinaryEntry>,
}

pub fn is_template(file: &str) -> bool {
    file.contains("@.")
}

impl BinaryEntry {
    /// The paths a host with `roles` runs this binary from, besides the user bin directory.
    pub fn install_paths_on(&self, roles: &BTreeSet<String>) -> Vec<&str> {
        self.install_paths
            .iter()
            .filter(|p| on_roles(&p.hosts, roles))
            .map(|p| p.path.as_str())
            .collect()
    }
}

impl UnitEntry {
    /// The directory under `deploy/` of the unit files; the empty string is `deploy/` itself.
    fn dir_name(&self) -> &str {
        match self.dir.as_deref() {
            None => "systemd",
            Some(".") => "",
            Some(dir) => dir,
        }
    }

    /// The repository directory of the unit files.
    pub fn source_dir(&self) -> String {
        match self.dir_name() {
            "" => "deploy".to_owned(),
            dir => format!("deploy/{dir}"),
        }
    }

    /// How coverage names one of its files: the path under `deploy/`, always with its directory, so
    /// `deploy/foo.service` (`foo.service`) and `deploy/systemd/foo.service` (`systemd/foo.service`)
    /// are two different files. Validation and coverage both use this one identity.
    pub fn coverage_key(&self, file: &str) -> String {
        match self.dir_name() {
            "" => file.to_owned(),
            dir => format!("{dir}/{file}"),
        }
    }

    /// The unit that must be active: `enable`, else the only timer, else the only file.
    pub fn enable_unit(&self) -> Result<&str, String> {
        if let Some(unit) = &self.enable {
            return if self.files.contains(unit) {
                Ok(unit)
            } else {
                Err(format!(
                    "{}: enable {unit} is not one of its files",
                    self.name
                ))
            };
        }
        let timers: Vec<&String> = self
            .files
            .iter()
            .filter(|f| f.trim_end_matches(".in").ends_with(".timer"))
            .collect();
        match (timers.as_slice(), self.files.as_slice()) {
            ([timer], _) => Ok(timer),
            ([], [only]) => Ok(only),
            _ => Err(format!(
                "{}: name the unit to enable with enable = ...",
                self.name
            )),
        }
    }

    /// The service the enabled timer starts (`foo.timer` starts `foo.service`), if the family has one.
    pub fn service_unit(&self) -> Option<String> {
        let enable = self.enable_unit().ok()?;
        let (stem, template) = match enable.strip_suffix(".in") {
            Some(stem) => (stem, ".in"),
            None => (enable, ""),
        };
        let service = stem
            .strip_suffix(".timer")
            .map(|s| format!("{s}.service{template}"))?;
        self.files.contains(&service).then_some(service)
    }
}

/// `true` when `hosts` names a role in `roles` (or the role `all`).
pub fn on_roles(hosts: &[String], roles: &BTreeSet<String>) -> bool {
    hosts.iter().any(|h| h == ALL || roles.contains(h))
}

impl Manifest {
    pub fn parse(text: &str) -> Result<Manifest, String> {
        let manifest: Manifest = toml::from_str(text).map_err(|e| format!("manifest: {e}"))?;
        manifest.validate()?;
        Ok(manifest)
    }

    pub fn load(path: &Path) -> Result<Manifest, String> {
        let text = std::fs::read_to_string(path).map_err(|e| format!("{}: {e}", path.display()))?;
        Self::parse(&text)
    }

    pub fn load_from_repo(repo: &Path) -> Result<Manifest, String> {
        Self::load(&repo.join(MANIFEST_PATH))
    }

    /// Every problem between the manifest and the tree: a unit file or binary target it does not name,
    /// an entry that names one that does not exist.
    pub fn coverage(
        &self,
        unit_files: &BTreeSet<String>,
        bins: &BTreeSet<(String, String)>,
    ) -> Vec<String> {
        let mut problems = Vec::new();
        let named: BTreeSet<String> = self
            .unit
            .iter()
            .flat_map(|u| u.files.iter().map(|f| u.coverage_key(f)))
            .collect();
        for file in unit_files {
            if !named.contains(file) {
                problems.push(format!(
                    "unit file deploy/{file} is not in {MANIFEST_PATH}: add it to a [[unit]] entry (dir = \"<subdir>\" outside deploy/systemd), or an entry with not_deployed = \"<reason>\""
                ));
            }
        }
        for file in &named {
            if !unit_files.contains(file) {
                problems.push(format!(
                    "{MANIFEST_PATH} names deploy/{file}, which is not a tracked file"
                ));
            }
        }
        let listed: BTreeSet<(String, String)> = self
            .binary
            .iter()
            .map(|b| (b.package.clone(), b.bin.clone()))
            .collect();
        for (package, bin) in bins {
            if !listed.contains(&(package.clone(), bin.clone())) {
                problems.push(format!(
                    "binary {package}/{bin} is not in {MANIFEST_PATH}: add a [[binary]] entry, or one with not_deployed = \"<reason>\""
                ));
            }
        }
        for (package, bin) in &listed {
            if !bins.contains(&(package.clone(), bin.clone())) {
                problems.push(format!(
                    "{MANIFEST_PATH} names binary {package}/{bin}, which no crate builds"
                ));
            }
        }
        problems
    }

    /// The deployed binaries a host with `roles` installs, as (package, bin).
    pub fn binaries_for(&self, roles: &BTreeSet<String>) -> Vec<(&str, &str)> {
        self.binary
            .iter()
            .filter(|b| b.not_deployed.is_none() && on_roles(&b.hosts, roles))
            .map(|b| (b.package.as_str(), b.bin.as_str()))
            .collect()
    }

    /// The deployed unit families a host with `roles` installs.
    pub fn units_for(&self, roles: &BTreeSet<String>) -> Vec<&UnitEntry> {
        self.unit
            .iter()
            .filter(|u| u.not_deployed.is_none() && on_roles(&u.hosts, roles))
            .collect()
    }
}

#[cfg(test)]
mod consumer_tests;
#[cfg(test)]
mod scan_tests;
#[cfg(test)]
mod tests;
