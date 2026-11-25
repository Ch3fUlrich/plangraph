//! The post-merge deploy step of a train (TRAIN-DEPLOYS-ITS-FAMILIES).
//!
//! After the merge, the targets named in the GO row's `deploy:` line are installed one by one. One run of
//! each is read; a keeper deploy row is written only after that first run succeeded. A failed first run
//! disables the unit's timer, writes no row and stops the step: the targets behind it stay untouched and
//! are reported. The step keeps a [`PostState`] of finished targets, so a rerun after an interruption
//! (the resumable hook of LAND-RESUMABLE-AFTER-MERGE) skips what is done and never redoes an install.
use super::deploy::{DeployPlan, Target};
pub use super::deploy_run::*;
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};

/// The timer the keeper's deploy gate wants active beside a oneshot service.
fn paired_timer(family: &str) -> Option<String> {
    family
        .strip_suffix(".service")
        .map(|b| format!("{b}.timer"))
}

/// Progress of the step, saved after each target: `family@host` of every finished target.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct PostState {
    #[serde(default)]
    pub done: BTreeSet<String>,
    /// The first failed target: the step stopped there and does not run again until it is cleared.
    #[serde(default)]
    pub failed: Option<String>,
    /// The train this progress belongs to (`DeployPlan::train_sha`); another train's state is refused.
    #[serde(default)]
    pub train: String,
}

fn name(t: &Target) -> String {
    format!("{}@{}", t.family, t.host)
}

#[derive(Debug, Default, PartialEq, Eq)]
pub struct PostReport {
    pub deployed: Vec<String>,
    pub skipped_done: Vec<String>,
    /// The failed target and why, when the step stopped.
    pub failed: Option<(String, String)>,
    /// Targets behind the failure, not touched.
    pub not_attempted: Vec<String>,
    pub notes: Vec<String>,
}

impl PostReport {
    pub fn lines(&self) -> Vec<String> {
        let mut out: Vec<String> = self
            .deployed
            .iter()
            .map(|d| format!("deployed {d}"))
            .collect();
        out.extend(
            self.skipped_done
                .iter()
                .map(|d| format!("already done {d}")),
        );
        if let Some((t, why)) = &self.failed {
            out.push(format!("failed {t}: {why}"));
        }
        out.extend(
            self.not_attempted
                .iter()
                .map(|d| format!("not attempted {d}")),
        );
        out.extend(self.notes.iter().cloned());
        out
    }
}

/// The plan targets a GO row's `deploy: unit@host, ...` line names. A name that is not a passing target of
/// the plan refuses the step: the GO may only deploy what the dry run passed.
pub fn named_targets(plan: &DeployPlan, go_row: &str) -> Result<Vec<Target>, String> {
    let list = go_row
        .trim()
        .strip_prefix("deploy:")
        .ok_or_else(|| format!("GO row {go_row:?} has no `deploy:` line"))?
        .trim();
    if list == "none" || list.is_empty() {
        return Ok(vec![]);
    }
    let by_name: BTreeMap<String, &Target> = plan.targets.iter().map(|t| (name(t), t)).collect();
    list.split(',')
        .map(|n| n.trim())
        .map(|n| {
            by_name.get(n).map(|t| (*t).clone()).ok_or_else(|| {
                format!("GO row names {n}, which is not a passing target of the plan")
            })
        })
        .collect()
}

/// A good oneshot run counts only with its timer active (the keeper gate). Returns the timer and the item
/// keys that need a timer row: the service's items less those the timer target writes itself. `None` when
/// the family is no oneshot.
fn paired_timer_row(
    installer: &dyn Installer,
    t: &Target,
    run: &RunRead,
    targets: &[Target],
) -> Result<Option<(String, BTreeSet<String>)>, String> {
    let Some(timer) = run.oneshot.then(|| paired_timer(&t.family)).flatten() else {
        return Ok(None);
    };
    let own: BTreeSet<&String> = targets
        .iter()
        .filter(|o| o.host == t.host && o.family == timer)
        .flat_map(|o| o.items.iter())
        .collect();
    let state = installer
        .read_run(&t.host, &timer)
        .map_err(|e| format!("paired timer {timer}: {e}"))?;
    if state.is_active != "active" {
        return Err(format!(
            "paired timer {timer} on {} is not active ({})",
            t.host, state.is_active
        ));
    }
    let keys = t
        .items
        .iter()
        .filter(|k| !own.contains(k))
        .cloned()
        .collect();
    Ok(Some((timer, keys)))
}

fn timer_row(key: &str, host: &str, unit: &str) -> DeployRow {
    DeployRow {
        key: key.to_owned(),
        host: host.to_owned(),
        unit: unit.to_owned(),
        is_active: "active".into(),
        kind: None,
        result: None,
        exec_main_exit_timestamp: None,
    }
}

/// The rows of one verified target: for a oneshot, first the paired timer's row for the items that need
/// one, then the service row.
fn write_rows(
    rows: &dyn RowSink,
    t: &Target,
    run: &RunRead,
    paired: &Option<(String, BTreeSet<String>)>,
) -> Result<(), String> {
    for key in &t.items {
        if let Some((timer, keys)) = paired {
            if keys.contains(key) {
                rows.write(&timer_row(key, &t.host, timer))
                    .map_err(|e| format!("deploy row {key} for {timer}: {e}"))?;
            }
        }
        rows.write(&DeployRow {
            key: key.clone(),
            host: t.host.clone(),
            unit: t.family.clone(),
            is_active: run.is_active.clone(),
            kind: run.oneshot.then(|| "oneshot".to_owned()),
            result: run.result.clone(),
            exec_main_exit_timestamp: run
                .exec_main_exit_timestamp
                .as_deref()
                .map(|t| exit_rfc3339(t).unwrap_or_else(|| t.to_owned())),
        })
        .map_err(|e| format!("deploy row {key} for {}: {e}", name(t)))?;
    }
    Ok(())
}

/// The service target a shipped timer target triggers on the same host, if the train ships both.
fn service_of<'a>(timer: &Target, targets: &'a [Target]) -> Option<&'a Target> {
    targets
        .iter()
        .find(|s| s.host == timer.host && paired_timer(&s.family).as_deref() == Some(&timer.family))
}

/// What the unit showed before this install: the exit time (microseconds) and invocation of its last run. A
/// unit that reports `absent` has none; a read that fails or an exit time that cannot be read leaves no
/// baseline to prove the run is new, so the target fails (before anything is installed).
fn baseline(installer: &dyn Installer, t: &Target) -> Result<Baseline, String> {
    let run = installer
        .read_run(&t.host, &t.family)
        .map_err(|e| format!("read before install: {e}"))?;
    if run.is_active == ABSENT {
        return Ok(Baseline::default());
    }
    let exit = match run.exec_main_exit_timestamp.as_deref() {
        None => None,
        Some(text) => Some(
            exit_micros(text)
                .ok_or_else(|| format!("exit time {text:?} before install is unreadable"))?,
        ),
    };
    Ok(Baseline {
        exit,
        invocation: run.invocation,
    })
}

/// A service whose paired timer the preflight refused is not deployed: found before any host is touched.
fn refused_timer<'a>(targets: &'a [Target], gate: &Gate) -> Option<(&'a Target, String)> {
    targets.iter().find_map(|t| {
        let timer = paired_timer(&t.family)?;
        gate.excluded
            .contains(&(timer.clone(), t.host.clone()))
            .then_some((t, timer))
    })
}

/// Installs `targets` (timers first). A timer whose service the train also ships is held: its rows and its
/// checkpoint wait until that service has passed, so a failed service leaves no timer evidence behind.
/// `checkpoint` is called after every state change so the caller can save it.
pub fn apply(
    targets: &[Target],
    gate: &Gate,
    installer: &dyn Installer,
    rows: &dyn RowSink,
    state: &mut PostState,
    checkpoint: &mut dyn FnMut(&PostState) -> Result<(), String>,
) -> Result<PostReport, String> {
    if let Some(f) = &state.failed {
        return Err(format!(
            "an earlier first run failed ({f}); clear it before a rerun"
        ));
    }
    let mut report = PostReport::default();
    if let Some((t, timer)) = refused_timer(targets, gate) {
        report.failed = Some((
            name(t),
            format!(
                "paired timer {timer} on {} was refused at preflight and is not in the GO",
                t.host
            ),
        ));
        report.not_attempted = targets
            .iter()
            .filter(|o| name(o) != name(t))
            .map(name)
            .collect();
        return Ok(report);
    }
    let mut ordered: Vec<&Target> = targets.iter().collect();
    ordered.sort_by_key(|t| !t.family.ends_with(".timer"));
    // Held timers by the id of the service that releases them.
    let mut held: BTreeMap<String, (Target, RunRead)> = BTreeMap::new();
    let release = |held: &mut BTreeMap<String, (Target, RunRead)>,
                   service: &str,
                   rows: &dyn RowSink,
                   state: &mut PostState,
                   report: &mut PostReport,
                   checkpoint: &mut dyn FnMut(&PostState) -> Result<(), String>|
     -> Result<(), String> {
        if let Some((timer, run)) = held.remove(service) {
            write_rows(rows, &timer, &run, &None)?;
            if timer.items.is_empty() {
                report.notes.push(format!(
                    "{} has no item key: no deploy row written",
                    name(&timer)
                ));
            }
            state.done.insert(name(&timer));
            checkpoint(state)?;
            report.deployed.push(name(&timer));
        }
        Ok(())
    };
    for (i, t) in ordered.iter().copied().enumerate() {
        let id = name(t);
        if state.done.contains(&id) {
            report.skipped_done.push(id.clone());
            release(&mut held, &id, rows, state, &mut report, checkpoint)?;
            continue;
        }
        // `changed`: the target's install was started, so it may have changed the host.
        let mut changed = false;
        let outcome = baseline(installer, t)
            .and_then(|base| {
                changed = true;
                let started = installer
                    .install(&t.host, &t.family)
                    .map_err(|e| format!("install: {e}"))?;
                let run = installer
                    .read_run(&t.host, &t.family)
                    .map_err(|e| format!("first run: {e}"))?;
                Ok((base, started, run))
            })
            .and_then(|(base, started, run)| {
                let now = (gate.clock)();
                if run.good(&t.family, gate, &base, now, started.as_deref()) {
                    paired_timer_row(installer, t, &run, targets).map(|timer| (run, timer))
                } else {
                    Err(format!(
                        "first run of {} on {} is not good (is_active={}, result={}, exited={}, invocation={}, started={})",
                        t.family,
                        t.host,
                        run.is_active,
                        run.result.as_deref().unwrap_or("-"),
                        run.exec_main_exit_timestamp.as_deref().unwrap_or("-"),
                        run.invocation.as_deref().unwrap_or("-"),
                        started.as_deref().unwrap_or("-")
                    ))
                }
            });
        match outcome {
            Ok((run, _)) if service_of(t, targets).is_some() => {
                let service = service_of(t, targets).map(name).unwrap_or_default();
                held.insert(service, (t.clone(), run));
            }
            Ok((run, paired)) => {
                write_rows(rows, t, &run, &paired)?;
                if t.items.is_empty() {
                    report
                        .notes
                        .push(format!("{id} has no item key: no deploy row written"));
                }
                state.done.insert(id.clone());
                checkpoint(state)?;
                report.deployed.push(id.clone());
                release(&mut held, &id, rows, state, &mut report, checkpoint)?;
            }
            Err(why) => {
                // Only what the GO names and this attempt installed is switched off: the timers held for
                // their service, and the failed unit itself when it triggers itself (a timer, socket, path).
                // A failure before the install changed nothing, so it leaves the rerun open.
                let mut off: Vec<(String, String)> = held
                    .values()
                    .map(|(h, _)| (h.host.clone(), h.family.clone()))
                    .collect();
                if changed && !t.family.ends_with(".service") && !t.family.starts_with("bin:") {
                    off.push((t.host.clone(), t.family.clone()));
                }
                for (host, unit) in off {
                    if let Err(e) = installer.disable(&host, &unit) {
                        report
                            .notes
                            .push(format!("could not disable {unit} on {host}: {e}"));
                    }
                }
                if changed {
                    state.failed = Some(id.clone());
                    checkpoint(state)?;
                }
                report.not_attempted = ordered[i + 1..]
                    .iter()
                    .map(|t| name(t))
                    .filter(|n| !state.done.contains(n))
                    .collect();
                report.failed = Some((id, why));
                return Ok(report);
            }
        }
    }
    Ok(report)
}
