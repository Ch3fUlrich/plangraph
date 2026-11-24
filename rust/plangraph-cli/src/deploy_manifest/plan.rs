//! The install plan of one host: what the manifest assigns to its roles and what is missing or outdated.
//!
//! Computed before anything changes (`host-sync --install --check` stops here). Rows come from the live
//! state ([`super::live::read_live`]) plus the files on disk: a unit file that is absent or differs from
//! the checkout, an env file that is absent. A root (system scope) unit is never installed; it is listed
//! as an operator step.
use super::install::missing_env;
use super::live::{read_live, HostFacts, Kind, Live};
use super::systemd::Systemctl;
use super::{is_template, Manifest, Scope};
use std::collections::BTreeSet;
use std::path::Path;

/// What to do about one entry.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Fix {
    /// Build and install the release binaries.
    Release,
    /// Copy the unit files, reload systemd, enable the timer or service.
    Units,
    /// Create the env files of a family from the documented defaults.
    EnvFamily(String),
    /// A named env file the tree cannot create (a secret, or provisioned by bootstrap).
    OperatorEnv(String),
    /// A root unit: never installed by this command.
    OperatorRoot,
    /// A binary a system unit runs from a path this command does not write: the operator copies the built
    /// artifact there (as root).
    OperatorBinary(String),
    /// Installed and enabled; nothing to do until the timer has started its service once and it succeeded.
    AwaitFirstRun,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Row {
    pub kind: Kind,
    pub name: String,
    pub before: String,
    pub fix: Fix,
}

impl Row {
    pub fn action(&self) -> String {
        match &self.fix {
            Fix::Release => "install release binaries".into(),
            Fix::Units => "install unit files and enable".into(),
            Fix::EnvFamily(family) => format!("create env files (family {family})"),
            Fix::OperatorEnv(name) => format!("OPERATOR: provide {name}"),
            Fix::OperatorRoot => "OPERATOR: install as root (system unit)".into(),
            Fix::OperatorBinary(path) => {
                format!("OPERATOR: copy the built binary to {path} (as root)")
            }
            Fix::AwaitFirstRun => "wait: the first run of the service is not done".into(),
        }
    }

    pub fn kind_text(&self) -> &'static str {
        if self.kind == Kind::Unit {
            "unit"
        } else {
            "binary"
        }
    }
}

/// The rows still to do; empty means the host is complete.
pub fn plan(
    manifest: &Manifest,
    roles: &BTreeSet<String>,
    repo: &Path,
    unit_dir: &Path,
    config_dir: &Path,
    sysctl: &dyn Systemctl,
    facts: &dyn HostFacts,
) -> Vec<Row> {
    let live = read_live(manifest, roles, sysctl, facts);
    let mut rows = Vec::new();
    for entry in live
        .iter()
        .filter(|e| !matches!(e.live, Live::Elsewhere(_)))
    {
        if entry.kind == Kind::Binary {
            if !entry.live.is_live() {
                // A binary run from a path outside the user bin directory needs the operator once the
                // current release is built; before that, the release build comes first.
                let consumer = manifest
                    .binary
                    .iter()
                    .find(|b| format!("{}/{}", b.package, b.bin) == entry.name)
                    .and_then(|b| b.install_path.clone().map(|p| (b.bin.clone(), p)));
                let fix = match consumer {
                    Some((bin, path))
                        if facts.source_tree().is_some()
                            && facts.artifact_record(&bin).map(|(stamp, _)| stamp)
                                == facts.source_tree() =>
                    {
                        Fix::OperatorBinary(path)
                    }
                    _ => Fix::Release,
                };
                rows.push(Row {
                    kind: Kind::Binary,
                    name: entry.name.clone(),
                    before: entry.live.text(),
                    fix,
                });
            }
            continue;
        }
        let Some(unit) = manifest.unit.iter().find(|u| u.name == entry.name) else {
            continue;
        };
        if unit.scope == Scope::System {
            if !entry.live.is_live() {
                rows.push(Row {
                    kind: Kind::Unit,
                    name: entry.name.clone(),
                    before: entry.live.text(),
                    fix: Fix::OperatorRoot,
                });
            }
            continue;
        }
        let mut notes = Vec::new();
        for file in &unit.files {
            let wanted = std::fs::read(repo.join(unit.source_dir()).join(file)).ok();
            match (std::fs::read(unit_dir.join(file)).ok(), wanted) {
                (None, _) => notes.push(format!("{file} missing")),
                (Some(have), Some(want)) if have != want => notes.push(format!("{file} outdated")),
                _ => {}
            }
        }
        let env_missing = missing_env(unit, config_dir);
        for name in &env_missing {
            let fix = unit
                .env_family
                .clone()
                .map_or_else(|| Fix::OperatorEnv(name.clone()), Fix::EnvFamily);
            rows.push(Row {
                kind: Kind::Unit,
                name: entry.name.clone(),
                before: format!("env file missing: {name}"),
                fix,
            });
        }
        let template = unit.files.iter().any(|f| is_template(f));
        if (!notes.is_empty() || (!entry.live.is_live() && !template))
            && env_missing.iter().all(|_| unit.env_family.is_some())
        {
            let before = if notes.is_empty() {
                entry.live.text()
            } else {
                notes.join(", ")
            };
            rows.push(Row {
                kind: Kind::Unit,
                name: entry.name.clone(),
                before,
                fix: Fix::Units,
            });
        }
    }
    rows
}

/// `plan<TAB>kind<TAB>name<TAB>before<TAB>action` per row.
pub fn plan_lines(rows: &[Row]) -> Vec<String> {
    rows.iter()
        .map(|r| {
            format!(
                "plan\t{}\t{}\t{}\t{}",
                r.kind_text(),
                r.name,
                r.before,
                r.action()
            )
        })
        .collect()
}
