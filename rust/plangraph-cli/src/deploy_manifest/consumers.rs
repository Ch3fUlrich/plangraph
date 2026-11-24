//! The manifest against how the unit files use it: the paths they run binaries from, the env files
//! they read and the installers of their templates. Reads files only; nothing here asks systemd.
use super::scan::Consumer;
use super::{on_roles, Manifest, UnitEntry, ALL, ROLES};
use std::collections::BTreeSet;
use std::path::Path;

impl Manifest {
    fn unit_by_key(&self, key: &str) -> Option<&UnitEntry> {
        self.unit
            .iter()
            .find(|u| u.files.iter().any(|f| u.coverage_key(f) == key))
    }

    /// Every problem between the binaries and the places units run them from. A program whose file name
    /// is a binary of this manifest (python, logger, claude are not) must be run from a path its entry
    /// declares (`~/.local/bin/<bin>` when it declares none), and on every role the unit is deployed to
    /// the binary must be installed there: the entry's hosts and the path's hosts both cover the unit's.
    pub fn consumer_problems(&self, consumers: &BTreeSet<Consumer>) -> Vec<String> {
        let mut problems = Vec::new();
        for Consumer { unit, bin, path } in consumers {
            let roles = self
                .unit_by_key(unit)
                .filter(|u| u.not_deployed.is_none())
                .map(|u| expand(&u.hosts));
            for entry in self.binary.iter().filter(|b| &b.bin == bin) {
                let everywhere = [ALL.to_owned()];
                let places: Vec<&[String]> = if entry.install_paths.is_empty() {
                    if path == &format!("~/.local/bin/{bin}") {
                        vec![&everywhere]
                    } else {
                        Vec::new()
                    }
                } else {
                    entry
                        .install_paths
                        .iter()
                        .filter(|p| &p.path == path)
                        .map(|p| p.hosts.as_slice())
                        .collect()
                };
                if places.is_empty() {
                    problems.push(format!(
                        "deploy/{unit} runs {path}, which binary {}/{bin} does not declare: add it to its install_paths (with ~/.local/bin/{bin} when that is used too)",
                        entry.package
                    ));
                    continue;
                }
                for role in roles.iter().flatten() {
                    let here = BTreeSet::from([role.clone()]);
                    let at_path = places.iter().any(|hosts| on_roles(hosts, &here));
                    if !at_path || !on_roles(&entry.hosts, &here) {
                        problems.push(format!(
                            "deploy/{unit} runs {path} on role {role}, where binary {}/{bin} is not installed there (its hosts or the install path's hosts omit {role})",
                            entry.package
                        ));
                    }
                }
            }
        }
        problems
    }

    /// Every problem between the unit files and the env files the manifest names for them (`env`: unit
    /// key and the file name its `EnvironmentFile=` reads): a file the unit reads that its entry omits.
    pub fn env_problems(&self, env: &BTreeSet<(String, String)>) -> Vec<String> {
        let mut problems = Vec::new();
        for (key, name) in env {
            let Some(entry) = self.unit_by_key(key) else {
                continue; // an unlisted unit file is reported by `coverage`
            };
            if !entry
                .env_files
                .iter()
                .flat_map(|e| e.split('|'))
                .any(|e| e == name)
            {
                problems.push(format!(
                    "deploy/{key} reads env file {name}, which unit {} does not list in env_files",
                    entry.name
                ));
            }
        }
        problems
    }

    /// Every problem with the installers: a template unit file (`*.in`, or with an `@NAME@` placeholder)
    /// whose entry names no installer, and an installer script that Git does not track.
    pub fn installer_problems(&self, repo: &Path, unit_files: &BTreeSet<String>) -> Vec<String> {
        let mut problems = Vec::new();
        for key in unit_files {
            let Some(unit) = self.unit_by_key(key) else {
                continue; // an unlisted unit file is reported by `coverage`
            };
            if unit.installer.is_some() || unit.not_deployed.is_some() {
                continue;
            }
            match super::scan::needs_rendering(repo, key) {
                Ok(true) => problems.push(format!(
                    "deploy/{key} is a template: its entry needs an installer (via, step, renders, enable)"
                )),
                Ok(false) => {}
                Err(e) => problems.push(e),
            }
        }
        for unit in &self.unit {
            let Some(installer) = &unit.installer else {
                continue;
            };
            // Tracked in the index and still on disk: a script deleted but not yet staged is not usable.
            match super::scan::tracked(repo, &installer.via) {
                Ok(files)
                    if files.contains(&installer.via) && repo.join(&installer.via).is_file() => {}
                Ok(files) if files.contains(&installer.via) => problems.push(format!(
                    "unit {}: installer via {} is tracked but missing from the working tree",
                    unit.name, installer.via
                )),
                Ok(_) => problems.push(format!(
                    "unit {}: installer via {} is not a tracked file of the repository",
                    unit.name, installer.via
                )),
                Err(e) => problems.push(e),
            }
        }
        problems
    }
}

/// The roles a `hosts` list names, `all` expanded.
fn expand(hosts: &[String]) -> BTreeSet<String> {
    if hosts.iter().any(|h| h == ALL) {
        ROLES.iter().map(|r| (*r).to_owned()).collect()
    } else {
        hosts.iter().cloned().collect()
    }
}
