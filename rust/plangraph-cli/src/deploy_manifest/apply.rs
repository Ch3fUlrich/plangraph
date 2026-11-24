//! The one install run of a host: plan, optionally stop (`--check`), install what is missing, check again,
//! write the ledger. Idempotent: a complete host gets the plan "nothing to do" and no step runs.
use super::install::{install_units, Target};
use super::ledger::Ledger;
use super::live::HostFacts;
use super::plan::{plan, plan_lines, Fix, Row};
use super::report::status_full;
use super::systemd::Systemctl;
use super::{Manifest, Scope};
use std::collections::BTreeSet;
use std::path::{Path, PathBuf};

/// The two things this module cannot do itself: build and install the release, create env defaults.
pub trait Steps {
    fn release(&self) -> Result<(), String>;
    fn env_family(&self, family: &str) -> Result<(), String>;
}

pub struct Host<'a> {
    pub repo: &'a Path,
    pub unit_dir: &'a Path,
    pub config_dir: &'a Path,
    pub ledger_path: Option<PathBuf>,
}

#[derive(Debug)]
pub struct Outcome {
    pub lines: Vec<String>,
    /// Something is still missing (after the install, or in `--check` mode before it).
    pub pending: bool,
    pub ledger: Ledger,
}

fn same(a: &Row, b: &Row) -> bool {
    a.kind == b.kind && a.name == b.name && a.fix == b.fix
}

pub fn run(
    manifest: &Manifest,
    roles: &BTreeSet<String>,
    host: &Host,
    sysctl: &dyn Systemctl,
    facts: &dyn HostFacts,
    steps: &dyn Steps,
    check_only: bool,
) -> Result<Outcome, String> {
    let rows = |sysctl: &dyn Systemctl| {
        plan(
            manifest,
            roles,
            host.repo,
            host.unit_dir,
            host.config_dir,
            sysctl,
            facts,
        )
    };
    let before = rows(sysctl);
    let mut lines = plan_lines(&before);
    let mut pending = !before.is_empty();
    if !before.is_empty() && !check_only {
        let mut failures = Vec::new();
        if before.iter().any(|r| r.fix == Fix::Release) {
            if let Err(error) = steps.release() {
                failures.push(format!("release install: {error}"));
            }
        }
        let families: BTreeSet<&String> = before
            .iter()
            .filter_map(|r| {
                if let Fix::EnvFamily(f) = &r.fix {
                    Some(f)
                } else {
                    None
                }
            })
            .collect();
        for family in families {
            if let Err(error) = steps.env_family(family) {
                failures.push(format!("env family {family}: {error}"));
            }
        }
        let target = Target {
            repo: host.repo,
            unit_dir: host.unit_dir,
            config_dir: host.config_dir,
            scope: Scope::User,
        };
        let report = install_units(manifest, roles, &target, sysctl);
        lines.extend(report.lines.iter().map(|l| format!("install\t{l}")));
        lines.extend(failures.iter().map(|f| format!("install\tFAIL {f}")));
        let after = rows(sysctl);
        for row in &before {
            let now = after.iter().find(|a| same(a, row));
            lines.push(format!(
                "result\t{}\t{}\t{}\t{}",
                row.kind_text(),
                row.name,
                row.before,
                now.map_or_else(|| "ok".to_owned(), |a| format!("still: {}", a.before))
            ));
        }
        // The installer's own pending flag counts too: a reload that failed after the files were copied
        // leaves the files equal to the checkout, so a re-read of the files alone would not see it.
        pending = !after.is_empty() || !failures.is_empty() || report.pending;
    }
    let (status, ledger) =
        status_full(host.repo, roles, sysctl, facts, host.ledger_path.as_deref())?;
    if before.is_empty() {
        let live = status
            .iter()
            .filter(|l| l.starts_with("deploy-entry") && l.contains("\tlive\t"))
            .count();
        lines.push(format!("nothing to do\t{live} entries live"));
    }
    lines.extend(
        status
            .into_iter()
            .filter(|l| l.starts_with("deploy-not-live")),
    );
    Ok(Outcome {
        lines,
        pending,
        ledger,
    })
}
