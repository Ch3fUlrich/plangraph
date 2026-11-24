//! Live state of the manifest entries on one host, read through [`read_live`] only.
//!
//! A unit is live when systemd has loaded it, it is enabled, its enabled unit is active and the service
//! it starts did not fail its last run. A binary is live when it is installed and the release stamp of this
//! host equals the tree the checkout holds. All reads go through `Systemctl` and `HostFacts`, so tests use
//! fixtures and never touch systemd, git or the file system of a real host.
use super::systemd::Systemctl;
use super::{is_template, BinaryEntry, Manifest, Scope, UnitEntry};
use std::collections::{BTreeMap, BTreeSet};

/// What the host says about itself apart from systemd.
pub trait HostFacts {
    fn binary_installed(&self, bin: &str) -> bool;
    /// The Rust trees the release installer has built and installed on this host (its stamps).
    fn built_trees(&self) -> Vec<String>;
    /// The Rust tree of the checkout the release installer builds from.
    fn source_tree(&self) -> Option<String>;
    /// When the newest commit touching `paths` reached the checkout's head, epoch seconds.
    fn merged_at(&self, paths: &[String]) -> Option<i64>;
    fn now(&self) -> i64;
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Live {
    Live,
    /// The unit file is not loaded by systemd, or the binary is not installed.
    NotInstalled,
    NotEnabled(String),
    NotActive(String),
    /// The service the timer starts ended in a failed state or reported a failed result.
    LastRunFailed(String),
    /// The installed binaries are older than the checkout, or no release was installed.
    Stale(String),
    /// systemd could not be read; the entry is counted as not live.
    Unreadable(String),
    /// Not asked of this host: another role, or `not_deployed`.
    Elsewhere(String),
}

impl Live {
    pub fn is_live(&self) -> bool {
        matches!(self, Live::Live | Live::Elsewhere(_))
    }

    pub fn text(&self) -> String {
        match self {
            Live::Live => "live".into(),
            Live::NotInstalled => "not-installed".into(),
            Live::NotEnabled(s) => format!("not-enabled({s})"),
            Live::NotActive(s) => format!("not-active({s})"),
            Live::LastRunFailed(s) => format!("last-run-failed({s})"),
            Live::Stale(s) => format!("stale({s})"),
            Live::Unreadable(s) => format!("unreadable({})", s.replace(['\n', '\t'], " ")),
            Live::Elsewhere(s) => format!("elsewhere({s})"),
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum Kind {
    Unit,
    Binary,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Entry {
    pub kind: Kind,
    pub name: String,
    pub live: Live,
    /// When the entry's change reached the checkout, if it can be told (units only).
    pub merged_at: Option<i64>,
    pub first_run: String,
}

impl Entry {
    /// The key of the entry in the ledger and in an exception: `unit:<name>` or `binary:<package>/<bin>`.
    pub fn key(&self) -> String {
        format!(
            "{}:{}",
            if self.kind == Kind::Unit {
                "unit"
            } else {
                "binary"
            },
            self.name
        )
    }
}

fn show(
    sysctl: &dyn Systemctl,
    scope: Scope,
    unit: &str,
) -> Result<BTreeMap<String, String>, String> {
    let out = sysctl.run(
        scope,
        &[
            "show",
            unit,
            "-p",
            "LoadState",
            "-p",
            "ActiveState",
            "-p",
            "UnitFileState",
            "-p",
            "Result",
        ],
    )?;
    if out.code != 0 {
        return Err(format!("systemctl show {unit}: {}", out.stderr.trim()));
    }
    Ok(out
        .stdout
        .lines()
        .filter_map(|l| l.split_once('='))
        .map(|(k, v)| (k.to_owned(), v.trim().to_owned()))
        .collect())
}

fn unit_live(entry: &UnitEntry, sysctl: &dyn Systemctl) -> Live {
    let enable = match entry.enable_unit() {
        Ok(unit) => unit,
        Err(error) => return Live::Unreadable(error),
    };
    let facts = match show(sysctl, entry.scope, enable) {
        Ok(facts) => facts,
        Err(error) => return Live::Unreadable(error),
    };
    let get = |k: &str| facts.get(k).map(String::as_str).unwrap_or("");
    if get("LoadState") != "loaded" {
        return Live::NotInstalled;
    }
    if is_template(enable) {
        return Live::Live;
    }
    let file_state = get("UnitFileState");
    if !(file_state.starts_with("enabled") || file_state == "static" || file_state == "alias") {
        return Live::NotEnabled(file_state.to_owned());
    }
    if get("ActiveState") != "active" {
        return Live::NotActive(get("ActiveState").to_owned());
    }
    let service = entry.service_unit().unwrap_or_else(|| enable.to_owned());
    let run = if service == enable {
        facts
    } else {
        match show(sysctl, entry.scope, &service) {
            Ok(facts) => facts,
            Err(error) => return Live::Unreadable(error),
        }
    };
    let (active, result) = (
        run.get("ActiveState").map(String::as_str).unwrap_or(""),
        run.get("Result").map(String::as_str).unwrap_or(""),
    );
    if active == "failed" || !(result.is_empty() || result == "success") {
        return Live::LastRunFailed(if result.is_empty() { active } else { result }.to_owned());
    }
    Live::Live
}

fn binary_live(entry: &BinaryEntry, facts: &dyn HostFacts) -> Live {
    if !facts.binary_installed(&entry.bin) {
        return Live::NotInstalled;
    }
    let (built, source) = (facts.built_trees(), facts.source_tree());
    match source {
        Some(tree) if !built.contains(&tree) => Live::Stale(format!(
            "installed release is not tree {}",
            &tree[..tree.len().min(12)]
        )),
        None if built.is_empty() => Live::Stale("no release stamp".into()),
        _ => Live::Live,
    }
}

/// Every manifest entry with its state on a host that holds `roles`.
pub fn read_live(
    manifest: &Manifest,
    roles: &BTreeSet<String>,
    sysctl: &dyn Systemctl,
    facts: &dyn HostFacts,
) -> Vec<Entry> {
    let mut out = Vec::new();
    for unit in &manifest.unit {
        let placed = unit.not_deployed.is_none() && super::on_roles(&unit.hosts, roles);
        let live = match (&unit.not_deployed, placed) {
            (Some(reason), _) => Live::Elsewhere(format!("not deployed: {reason}")),
            (None, false) => Live::Elsewhere(format!("roles {}", unit.hosts.join(","))),
            (None, true) => unit_live(unit, sysctl),
        };
        let paths: Vec<String> = unit
            .files
            .iter()
            .map(|f| format!("deploy/systemd/{f}"))
            .collect();
        out.push(Entry {
            kind: Kind::Unit,
            name: unit.name.clone(),
            merged_at: if placed {
                facts.merged_at(&paths)
            } else {
                None
            },
            live,
            first_run: unit.first_run.clone().unwrap_or_default(),
        });
    }
    for binary in &manifest.binary {
        let placed = binary.not_deployed.is_none() && super::on_roles(&binary.hosts, roles);
        let live = match (&binary.not_deployed, placed) {
            (Some(reason), _) => Live::Elsewhere(format!("not deployed: {reason}")),
            (None, false) => Live::Elsewhere(format!("roles {}", binary.hosts.join(","))),
            (None, true) => binary_live(binary, facts),
        };
        out.push(Entry {
            kind: Kind::Binary,
            name: format!("{}/{}", binary.package, binary.bin),
            merged_at: None,
            live,
            first_run: binary.first_run.clone().unwrap_or_default(),
        });
    }
    out
}
