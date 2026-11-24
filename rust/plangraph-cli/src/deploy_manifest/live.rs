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
    /// The hash of the executable the binary's consumer runs (`install_path`, else the user bin directory);
    /// `None` when it is missing or not executable.
    fn installed_sha(&self, entry: &BinaryEntry) -> Option<String>;
    /// `(release stamp, artifact hash)` the release installer recorded for `bin`.
    fn artifact_record(&self, bin: &str) -> Option<(String, String)>;
    /// The release stamp of the checkout the release installer builds from.
    fn source_tree(&self) -> Option<String>;
    /// When the newest commit touching `paths` reached the checkout's head, epoch seconds.
    fn merged_at(&self, paths: &[String]) -> Option<i64>;
    /// When `file` of the unit family was last written into the unit directory, epoch seconds.
    fn unit_file_changed_at(&self, _entry: &UnitEntry, _file: &str) -> Option<i64> {
        None
    }
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
    /// The unit files changed on disk and systemd has not reloaded them (`NeedDaemonReload=yes`).
    NeedsReload,
    /// The service of an active timer is not loaded, or has never run: nothing shows the unit works yet.
    NeverRun(String),
    /// The installed file is not the artifact built from this checkout's release, or the unit runs an older
    /// revision of its file than the one installed.
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
            Live::NeedsReload => "needs-daemon-reload".into(),
            Live::NeverRun(s) => format!("never-run({s})"),
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

/// The properties read of every unit.
pub const SHOW_PROPS: [&str; 8] = [
    "LoadState",
    "ActiveState",
    "UnitFileState",
    "Result",
    "NeedDaemonReload",
    "ExecMainStartTimestamp",
    "ExecMainStatus",
    "ActiveEnterTimestamp",
];

/// `systemctl show --timestamp=unix` prints `@<epoch>`; anything else (`n/a`, empty) is no time.
fn unix(value: &str) -> Option<i64> {
    value.strip_prefix('@')?.parse().ok()
}

fn show(
    sysctl: &dyn Systemctl,
    scope: Scope,
    unit: &str,
) -> Result<BTreeMap<String, String>, String> {
    let mut args = vec!["show", "--timestamp=unix", unit];
    for prop in SHOW_PROPS {
        args.extend(["-p", prop]);
    }
    let out = sysctl.run(scope, &args)?;
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

fn unit_live(entry: &UnitEntry, sysctl: &dyn Systemctl, host: &dyn HostFacts) -> Live {
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
    if get("NeedDaemonReload") == "yes" {
        return Live::NeedsReload;
    }
    let file_state = get("UnitFileState");
    if !(file_state.starts_with("enabled") || file_state == "static" || file_state == "alias") {
        return Live::NotEnabled(file_state.to_owned());
    }
    if get("ActiveState") != "active" {
        return Live::NotActive(get("ActiveState").to_owned());
    }
    // The running unit must have started after its file was last written: otherwise the old process or
    // schedule is still in charge of a revision that was only copied and reloaded.
    if let Some(written) = host.unit_file_changed_at(entry, enable) {
        if unix(get("ActiveEnterTimestamp")).is_none_or(|entered| entered < written) {
            return Live::Stale(format!(
                "{enable} runs a revision older than its installed file"
            ));
        }
    }
    let service = entry.service_unit().unwrap_or_else(|| enable.to_owned());
    if service == enable {
        // A service that is itself the enabled unit is running: that is its proof.
        return Live::Live;
    }
    // A timer proves nothing alone: the service it starts must be loaded, need no reload, and have run
    // to success after its own file was last written.
    let run = match show(sysctl, entry.scope, &service) {
        Ok(facts) => facts,
        Err(error) => return Live::Unreadable(error),
    };
    let run_get = |k: &str| run.get(k).map(String::as_str).unwrap_or("");
    if run_get("LoadState") != "loaded" {
        return Live::NeverRun(format!("{service} is not loaded"));
    }
    if run_get("NeedDaemonReload") == "yes" {
        return Live::NeedsReload;
    }
    let (active, result) = (run_get("ActiveState"), run_get("Result"));
    let status = run_get("ExecMainStatus");
    if active == "failed"
        || (result != "success" && !result.is_empty())
        || !matches!(status, "" | "0")
    {
        let why = if result != "success" && !result.is_empty() {
            result
        } else if !matches!(status, "" | "0") {
            status
        } else {
            active
        };
        return Live::LastRunFailed(why.to_owned());
    }
    let started = run_get("ExecMainStartTimestamp");
    if started.is_empty() {
        return Live::NeverRun(format!("{service} has not run since it was installed"));
    }
    let Some(started) = unix(started) else {
        return Live::NeverRun(format!("{service} has an unreadable start time"));
    };
    if host
        .unit_file_changed_at(entry, &service)
        .is_some_and(|written| started < written)
    {
        return Live::NeverRun(format!(
            "{service} last ran before its installed file changed"
        ));
    }
    Live::Live
}

fn binary_live(entry: &BinaryEntry, host: &dyn HostFacts) -> Live {
    let Some(installed) = host.installed_sha(entry) else {
        return Live::NotInstalled;
    };
    let short = |t: &str| t[..t.len().min(12)].to_owned();
    let Some(source) = host.source_tree() else {
        return Live::Stale("the checkout's release stamp cannot be read".into());
    };
    match host.artifact_record(&entry.bin) {
        None => Live::Stale("no release record for this binary".into()),
        Some((stamp, _)) if stamp != source => Live::Stale(format!(
            "built from release {}, the checkout is at {}",
            short(&stamp),
            short(&source)
        )),
        Some((_, built)) if built != installed => Live::Stale(format!(
            "{} is not the artifact built for this release",
            entry
                .install_path
                .as_deref()
                .unwrap_or("the installed file")
        )),
        Some(_) => Live::Live,
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
            (None, true) => unit_live(unit, sysctl, facts),
        };
        let paths: Vec<String> = unit
            .files
            .iter()
            .map(|f| format!("{}/{f}", unit.source_dir()))
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
