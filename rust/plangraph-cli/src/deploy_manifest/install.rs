//! Install and enable the unit families of the manifest that belong on this host's roles.
//!
//! Unit files are copied from their `deploy/<dir>` (`deploy/systemd` by default) into the unit directory only when their content differs; a
//! family whose required env file is missing is reported (`NEEDS-ENV`) and not enabled, so no unit starts
//! without its configuration. Template units are installed but never enabled (an instance is enabled per
//! worker or role). The scope of the run (user or system) selects which entries it handles.
use super::systemd::Systemctl;
use super::{is_template, Manifest, Scope, UnitEntry};
use std::collections::BTreeSet;
use std::fs;
use std::path::Path;

/// Where a run installs: the checkout it copies from, the directories it writes to.
pub struct Target<'a> {
    pub repo: &'a Path,
    pub unit_dir: &'a Path,
    pub config_dir: &'a Path,
    pub scope: Scope,
}

/// `OK`/`FAIL` lines for the run and whether anything is still pending.
#[derive(Debug, Default, PartialEq, Eq)]
pub struct Report {
    pub lines: Vec<String>,
    pub pending: bool,
}

fn copy_if_changed(from: &Path, to: &Path) -> Result<bool, String> {
    let new = fs::read(from).map_err(|e| format!("{}: {e}", from.display()))?;
    if fs::read(to).is_ok_and(|old| old == new) {
        return Ok(false);
    }
    fs::create_dir_all(to.parent().ok_or("unit path has no parent")?).map_err(|e| e.to_string())?;
    fs::write(to, new).map_err(|e| format!("{}: {e}", to.display()))?;
    Ok(true)
}

/// Env files a family reads whose names carry an instance placeholder (`INSTANCE`, `ROLE`) are made per
/// instance, so a missing one is not the family's fault. `a|b` is satisfied by either file.
pub fn missing_env(entry: &UnitEntry, config_dir: &Path) -> Vec<String> {
    entry
        .env_files
        .iter()
        .filter(|name| !name.contains("INSTANCE") && !name.contains("ROLE"))
        .filter(|name| !name.split('|').any(|file| config_dir.join(file).is_file()))
        .cloned()
        .collect()
}

fn install_entry(entry: &UnitEntry, target: &Target, sysctl: &dyn Systemctl, report: &mut Report) {
    let name = &entry.name;
    let mut changed = false;
    for file in &entry.files {
        let from = target.repo.join(entry.source_dir()).join(file);
        match copy_if_changed(&from, &target.unit_dir.join(file)) {
            Ok(did) => changed |= did,
            Err(error) => {
                report.lines.push(format!("FAIL {name}: {error}"));
                report.pending = true;
                return;
            }
        }
    }
    if entry.files.iter().any(|f| is_template(f)) {
        report.lines.push(format!(
            "OK {name}: template installed; enable one instance per worker or role"
        ));
        return;
    }
    let missing = missing_env(entry, target.config_dir);
    if !missing.is_empty() {
        report
            .lines
            .push(format!("NEEDS-ENV {name}: {}", missing.join(", ")));
        report.pending = true;
        return;
    }
    let enable = match entry.enable_unit() {
        Ok(unit) => unit,
        Err(error) => {
            report.lines.push(format!("FAIL {error}"));
            report.pending = true;
            return;
        }
    };
    // A reload that failed on an earlier run left files equal to the checkout and systemd on the old text:
    // systemd itself says so (NeedDaemonReload), so the retry reloads even though no file changed.
    let reload_pending = sysctl
        .run(target.scope, &["show", enable, "-p", "NeedDaemonReload"])
        .is_ok_and(|out| out.stdout.contains("NeedDaemonReload=yes"));
    if changed || reload_pending {
        match sysctl.run(target.scope, &["daemon-reload"]) {
            Ok(out) if out.code == 0 => {}
            other => {
                report
                    .lines
                    .push(format!("FAIL {name}: daemon-reload {other:?}"));
                report.pending = true;
                return;
            }
        }
    }
    let state = |verb: &str| {
        sysctl
            .run(target.scope, &[verb, enable])
            .map(|o| o.stdout.trim().to_owned())
    };
    let enabled = state("is-enabled").is_ok_and(|s| s.starts_with("enabled") || s == "static");
    let active = state("is-active").is_ok_and(|s| s == "active");
    if enabled && active {
        report.lines.push(format!(
            "SAME {name}: {enable} enabled and active{}",
            if changed { " (files updated)" } else { "" }
        ));
        return;
    }
    match sysctl.run(target.scope, &["enable", "--now", enable]) {
        Ok(out) if out.code == 0 => report.lines.push(format!("ENABLED {name}: {enable}")),
        other => {
            report
                .lines
                .push(format!("FAIL {name}: enable --now {enable} {other:?}"));
            report.pending = true;
        }
    }
}

/// Install every deployed unit family of `roles` whose scope matches the target.
pub fn install_units(
    manifest: &Manifest,
    roles: &BTreeSet<String>,
    target: &Target,
    sysctl: &dyn Systemctl,
) -> Report {
    let mut report = Report::default();
    for entry in manifest.units_for(roles) {
        if entry.scope != target.scope {
            report.lines.push(format!(
                "SKIP {}: {:?} scope unit; run host-sync --install-units --scope system as root",
                entry.name, entry.scope
            ));
            continue;
        }
        install_entry(entry, target, sysctl, &mut report);
    }
    report
}
