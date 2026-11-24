//! The deploy manifest: the one table that names every systemd unit and every host binary the tree
//! builds, the host roles each belongs on, the env file names it reads and its first-run check.
//!
//! `deploy/manifest.toml` is read by `host-sync` (what to install), by `deploy-status` (what is live)
//! and by the CI coverage test (a unit file or binary target the manifest does not name fails the build).
//! An entry is either deployed (roles + first-run check) or `not_deployed = "<reason>"`; that is the
//! only way to leave a unit or binary out. The file names roles, never host names, and only env file
//! names, never values; a host learns its roles from `PG_HOST_ROLES` or `~/.config/plangraph/host-roles`.
use serde::Deserialize;
use std::collections::BTreeSet;
use std::path::Path;

pub mod install;
pub mod scan;
pub mod systemd;

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
    /// File names under `deploy/systemd`.
    pub files: Vec<String>,
    /// The unit that is enabled and must be active: the timer of a timer family, else the service.
    pub enable: Option<String>,
    #[serde(default)]
    pub scope: Scope,
    #[serde(default)]
    pub hosts: Vec<String>,
    /// Names of the files under the config directory the unit reads; never their values.
    #[serde(default)]
    pub env_files: Vec<String>,
    /// What proves the unit works after the first start; the status also reads the last run result.
    pub first_run: Option<String>,
    pub not_deployed: Option<String>,
}

/// One binary target of a workspace crate.
#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BinaryEntry {
    /// The Cargo package that builds it.
    pub package: String,
    pub bin: String,
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

impl UnitEntry {
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
            .filter(|f| f.ends_with(".timer"))
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
        let service = enable
            .strip_suffix(".timer")
            .map(|s| format!("{s}.service"))?;
        self.files.contains(&service).then_some(service)
    }
}

/// `true` when `hosts` names a role in `roles` (or the role `all`).
pub fn on_roles(hosts: &[String], roles: &BTreeSet<String>) -> bool {
    hosts.iter().any(|h| h == ALL || roles.contains(h))
}

fn check_hosts(what: &str, hosts: &[String]) -> Result<(), String> {
    for host in hosts {
        if host != ALL && !ROLES.contains(&host.as_str()) {
            return Err(format!(
                "{what}: unknown host role {host:?} (roles: all, {})",
                ROLES.join(", ")
            ));
        }
    }
    Ok(())
}

fn check_disposition(
    what: &str,
    hosts: &[String],
    first_run: &Option<String>,
    not_deployed: &Option<String>,
) -> Result<(), String> {
    check_hosts(what, hosts)?;
    match not_deployed {
        Some(reason) if reason.trim().is_empty() => {
            Err(format!("{what}: not_deployed needs a reason"))
        }
        Some(_) if !hosts.is_empty() || first_run.is_some() => {
            Err(format!("{what}: not_deployed excludes hosts and first_run"))
        }
        Some(_) => Ok(()),
        None if hosts.is_empty() => Err(format!(
            "{what}: name hosts or give not_deployed = \"<reason>\""
        )),
        None if first_run.as_deref().is_none_or(|c| c.trim().is_empty()) => {
            Err(format!("{what}: a deployed entry needs a first_run check"))
        }
        None => Ok(()),
    }
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

    fn validate(&self) -> Result<(), String> {
        let mut names = BTreeSet::new();
        let mut files = BTreeSet::new();
        for unit in &self.unit {
            let what = format!("unit {}", unit.name);
            if !names.insert(unit.name.as_str()) {
                return Err(format!("{what}: listed twice"));
            }
            if unit.files.is_empty() {
                return Err(format!("{what}: no files"));
            }
            for file in &unit.files {
                if !files.insert(file.as_str()) {
                    return Err(format!("{what}: {file} belongs to two entries"));
                }
            }
            unit.enable_unit()?;
            for env in &unit.env_files {
                if env.starts_with('/')
                    || env.contains(['=', ' ', '\\'])
                    || env.contains("..")
                    || !env.ends_with(".env")
                {
                    return Err(format!(
                        "{what}: env file {env:?} must be a relative file name ending in .env"
                    ));
                }
            }
            check_disposition(&what, &unit.hosts, &unit.first_run, &unit.not_deployed)?;
        }
        let mut bins = BTreeSet::new();
        for binary in &self.binary {
            let what = format!("binary {}/{}", binary.package, binary.bin);
            if !bins.insert((binary.package.as_str(), binary.bin.as_str())) {
                return Err(format!("{what}: listed twice"));
            }
            check_disposition(
                &what,
                &binary.hosts,
                &binary.first_run,
                &binary.not_deployed,
            )?;
        }
        Ok(())
    }

    /// Every problem between the manifest and the tree: a unit file or binary target it does not name,
    /// an entry that names one that does not exist.
    pub fn coverage(
        &self,
        unit_files: &BTreeSet<String>,
        bins: &BTreeSet<(String, String)>,
    ) -> Vec<String> {
        let mut problems = Vec::new();
        let named: BTreeSet<&String> = self.unit.iter().flat_map(|u| &u.files).collect();
        for file in unit_files {
            if !named.contains(file) {
                problems.push(format!(
                    "unit file deploy/systemd/{file} is not in {MANIFEST_PATH}: add it to a [[unit]] entry, or an entry with not_deployed = \"<reason>\""
                ));
            }
        }
        for file in named {
            if !unit_files.contains(file) {
                problems.push(format!(
                    "{MANIFEST_PATH} names deploy/systemd/{file}, which does not exist"
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

/// The roles of this host: `PG_HOST_ROLES` (comma or space separated), else the words of
/// `<config>/plangraph/host-roles`. Unknown words are an error, so a typo never silently drops a role.
pub fn host_roles(
    env_value: Option<&str>,
    file_text: Option<&str>,
) -> Result<BTreeSet<String>, String> {
    let text = match (env_value.filter(|v| !v.trim().is_empty()), file_text) {
        (Some(v), _) => v,
        (None, Some(f)) => f,
        (None, None) => "",
    };
    let mut roles = BTreeSet::new();
    for word in text
        .split(|c: char| c == ',' || c.is_whitespace())
        .filter(|w| !w.is_empty())
    {
        if !ROLES.contains(&word) {
            return Err(format!(
                "unknown host role {word:?} (roles: {})",
                ROLES.join(", ")
            ));
        }
        roles.insert(word.to_owned());
    }
    Ok(roles)
}

/// The roles of the host that runs this process; `config_dir` is `~/.config/plangraph`.
pub fn this_host_roles(config_dir: &Path) -> Result<BTreeSet<String>, String> {
    let file = std::fs::read_to_string(config_dir.join("host-roles")).ok();
    host_roles(
        std::env::var("PG_HOST_ROLES").ok().as_deref(),
        file.as_deref(),
    )
}

#[cfg(test)]
mod install_tests;
#[cfg(test)]
mod tests;
