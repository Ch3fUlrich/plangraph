//! The checks `Manifest::parse` runs on an entry before anything else reads it.
use super::{Manifest, ALL, ROLES};
use std::collections::BTreeSet;

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
    pub(super) fn validate(&self) -> Result<(), String> {
        let mut names = BTreeSet::new();
        let mut files: BTreeSet<String> = BTreeSet::new();
        for unit in &self.unit {
            let what = format!("unit {}", unit.name);
            if !names.insert(unit.name.as_str()) {
                return Err(format!("{what}: listed twice"));
            }
            if unit.files.is_empty() {
                return Err(format!("{what}: no files"));
            }
            if unit.dir.as_deref().is_some_and(|d| {
                d != "."
                    && (d.is_empty()
                        || d.starts_with('/')
                        || d.ends_with('/')
                        || d.split('/').any(|c| c.is_empty() || c == "." || c == ".."))
            }) {
                return Err(format!("{what}: dir must be a relative path under deploy/"));
            }
            if let Some(installer) = &unit.installer {
                installer.validate(&what)?;
            }
            for file in &unit.files {
                if file.is_empty() || file.contains(['/', '\\']) || file == ".." {
                    return Err(format!(
                        "{what}: file {file:?} must be a bare file name; name its directory in dir"
                    ));
                }
                if !files.insert(unit.coverage_key(file)) {
                    return Err(format!("{what}: {file} belongs to two entries"));
                }
            }
            unit.enable_unit()?;
            for env in unit.env_files.iter().flat_map(|e| e.split('|')) {
                // A file name under the config directory, or a path a unit names as written: absolute
                // or under `%h`.
                if env.contains(['=', ' ', '\\'])
                    || env.split('/').any(|part| part == "..")
                    || !env.ends_with(".env")
                    || env.starts_with("//")
                {
                    return Err(format!(
                        "{what}: env file {env:?} must be a file name or an absolute or %h path ending in .env"
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
            for place in &binary.install_paths {
                let abs = place.path.starts_with('/') || place.path.starts_with("~/");
                if !abs || place.path.contains("..") || place.path.ends_with('/') {
                    return Err(format!(
                        "{what}: install path {:?} must be an absolute file path or start with ~/",
                        place.path
                    ));
                }
                if place.hosts.is_empty() {
                    return Err(format!(
                        "{what}: install path {} names no hosts",
                        place.path
                    ));
                }
                check_hosts(
                    &format!("{what}: install path {}", place.path),
                    &place.hosts,
                )?;
                if binary.not_deployed.is_some() {
                    return Err(format!("{what}: not_deployed excludes install_paths"));
                }
            }
            let mut seen = BTreeSet::new();
            for place in &binary.install_paths {
                if !seen.insert(place.path.as_str()) {
                    return Err(format!("{what}: install path {} listed twice", place.path));
                }
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
}
