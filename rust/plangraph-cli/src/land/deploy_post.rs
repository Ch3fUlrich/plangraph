//! The standalone deploy runner (TRAIN-DEPLOYS-ITS-FAMILIES): installs the targets of a given plan one by
//! one, reads one run of each and writes the keeper deploy rows only after that first run is proven. A failed
//! first run disables what this attempt installed and the GO names, writes no row, saves the failure and
//! stops the step; the targets behind it stay untouched. A unit that could not be switched off is saved
//! too, and the next run retries it (only if the current plan names it) before anything else. A
//! [`PostState`] of finished targets lets a rerun skip them. Settlement wiring, GO binding and landed-path
//! derivation are other items.
pub use super::deploy_run::*;
use std::collections::{BTreeMap, BTreeSet};

/// The timer the keeper's deploy gate wants active beside a oneshot service.
fn paired_timer(family: &str) -> Option<String> {
    family
        .strip_suffix(".service")
        .map(|b| format!("{b}.timer"))
}

fn name(t: &RunTarget) -> String {
    format!("{}@{}", t.family, t.host)
}

/// A good oneshot run counts only with its timer active (the keeper gate). Returns the timer and the item
/// keys that need a timer row: the service's items less those the timer target writes itself. `None` when
/// the family is no oneshot.
fn paired_timer_row(
    installer: &dyn Installer,
    t: &RunTarget,
    run: &RunRead,
    targets: &[RunTarget],
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
    t: &RunTarget,
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
fn service_of<'a>(timer: &RunTarget, targets: &'a [RunTarget]) -> Option<&'a RunTarget> {
    targets
        .iter()
        .find(|s| s.host == timer.host && paired_timer(&s.family).as_deref() == Some(&timer.family))
}

/// What the unit showed before this install: the exit time (microseconds) and invocation of its last run. A
/// unit that reports `absent` has none; a read that fails or an exit time that cannot be read leaves no
/// baseline to prove the run is new, so the target fails (before anything is installed).
fn baseline(installer: &dyn Installer, t: &RunTarget) -> Result<Baseline, String> {
    let run = installer
        .read_run(&t.host, &t.family)
        .map_err(|e| format!("read before install: {e}"))?;
    if run.is_active == ABSENT {
        return Ok(Baseline::default());
    }
    let active = matches!(
        run.is_active.as_str(),
        "active" | "activating" | "reloading"
    );
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
        active,
    })
}

/// Switches off one unit; a failure is kept in the state, so the next run retries it first.
fn switch_off(
    installer: &dyn Installer,
    state: &mut PostState,
    report: &mut PostReport,
    host: &str,
    unit: &str,
) {
    if let Err(e) = installer.disable(host, unit) {
        report
            .notes
            .push(format!("could not disable {unit} on {host}: {e}"));
        state.cleanup.insert((host.to_owned(), unit.to_owned()));
    }
}

/// The identity of a plan on a landed revision: a digest of the revision, the targets and their items. A state belongs to the plan that first
/// used it, so a checkpoint of another plan never decides what this plan switches off or skips.
pub fn plan_id(targets: &[RunTarget], revision: &str) -> String {
    use sha2::{Digest, Sha256};
    let mut lines: Vec<String> = targets
        .iter()
        .map(|t| {
            let items: Vec<&str> = t.items.iter().map(String::as_str).collect();
            format!("{} {}", name(t), items.join(","))
        })
        .collect();
    lines.sort();
    lines.insert(0, format!("revision {revision}"));
    format!("{:x}", Sha256::digest(lines.join("\n")))
}

/// Cleanup after a failure once the host may have changed: switches off the timers held for their service
/// and `own` when it triggers itself (a timer, socket, path), and saves any disable that failed.
fn abort(
    installer: &dyn Installer,
    state: &mut PostState,
    report: &mut PostReport,
    held: &BTreeMap<String, (RunTarget, RunRead)>,
    own: Option<&RunTarget>,
    checkpoint: &mut dyn FnMut(&PostState) -> Result<(), String>,
) -> Result<(), String> {
    let mut off: Vec<(String, String)> = held
        .values()
        .map(|(h, _)| (h.host.clone(), h.family.clone()))
        .collect();
    if let Some(t) =
        own.filter(|t| !t.family.ends_with(".service") && !t.family.starts_with("bin:"))
    {
        off.push((t.host.clone(), t.family.clone()));
    }
    for (host, unit) in off {
        switch_off(installer, state, report, &host, &unit);
    }
    if !state.cleanup.is_empty() {
        checkpoint(state)?;
    }
    Ok(())
}

/// Installs `targets` (timers first). A timer whose service the train also ships is held: its rows and its
/// checkpoint wait until that service has passed, so a failed service leaves no timer evidence behind.
/// `checkpoint` is called after every state change so the caller can save it.
pub fn apply(
    targets: &[RunTarget],
    gate: &Gate,
    installer: &dyn Installer,
    rows: &dyn RowSink,
    state: &mut PostState,
    checkpoint: &mut dyn FnMut(&PostState) -> Result<(), String>,
) -> Result<PostReport, String> {
    let mut report = PostReport::default();
    // The state belongs to one plan: a checkpoint of another plan (or of an unknown one) decides nothing.
    if gate.revision.trim().is_empty() {
        return Err("the gate names no landed revision to bind the state to".into());
    }
    let pid = plan_id(targets, &gate.revision);
    if state.plan.is_empty() {
        if !state.done.is_empty() || !state.cleanup.is_empty() || state.failed.is_some() {
            return Err("the state names no plan; fix or clear it".into());
        }
        state.plan = pid;
    } else if state.plan != pid {
        return Err(format!(
            "the state belongs to plan {}, this plan is {pid}; clear it for a new plan",
            state.plan
        ));
    }
    // A unit an earlier attempt could not switch off is retried before anything else, and the step goes on
    // only when it is off. Every pending (host, unit) pair must be a target of the current plan before any
    // disable command is issued.
    let planned: BTreeSet<(&str, &str)> = targets
        .iter()
        .map(|t| (t.host.as_str(), t.family.as_str()))
        .collect();
    if let Some((host, unit)) = state
        .cleanup
        .iter()
        .find(|(h, u)| !planned.contains(&(h.as_str(), u.as_str())))
    {
        return Err(format!(
            "pending cleanup {unit}@{host} is not a target of this plan; fix or clear the state"
        ));
    }
    let pending = std::mem::take(&mut state.cleanup);
    let had_pending = !pending.is_empty();
    for (host, unit) in pending {
        switch_off(installer, state, &mut report, &host, &unit);
    }
    if had_pending {
        checkpoint(state)?;
    }
    if !state.cleanup.is_empty() {
        let left: Vec<String> = state
            .cleanup
            .iter()
            .map(|(h, u)| format!("{u}@{h}"))
            .collect();
        return Err(format!(
            "an earlier attempt could not switch off {}; fix the host and rerun",
            left.join(", ")
        ));
    }
    if let Some(f) = &state.failed {
        return Err(format!(
            "an earlier first run failed ({f}); clear it before a rerun"
        ));
    }
    let mut ordered: Vec<&RunTarget> = targets.iter().collect();
    ordered.sort_by_key(|t| !t.family.ends_with(".timer"));
    // Held timers by the id of the service that releases them.
    let mut held: BTreeMap<String, (RunTarget, RunRead)> = BTreeMap::new();
    let release = |held: &mut BTreeMap<String, (RunTarget, RunRead)>,
                   service: &str,
                   rows: &dyn RowSink,
                   state: &mut PostState,
                   report: &mut PostReport,
                   checkpoint: &mut dyn FnMut(&PostState) -> Result<(), String>|
     -> Result<(), String> {
        if let Some((timer, run)) = held.remove(service) {
            if let Err(e) = write_rows(rows, &timer, &run, &None) {
                held.insert(service.to_owned(), (timer, run));
                return Err(e);
            }
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
            if let Err(e) = release(&mut held, &id, rows, state, &mut report, checkpoint) {
                abort(installer, state, &mut report, &held, None, checkpoint)?;
                return Err(e);
            }
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
                if let Err(e) = write_rows(rows, t, &run, &paired) {
                    abort(installer, state, &mut report, &held, Some(t), checkpoint)?;
                    return Err(e);
                }
                if t.items.is_empty() {
                    report
                        .notes
                        .push(format!("{id} has no item key: no deploy row written"));
                }
                state.done.insert(id.clone());
                checkpoint(state)?;
                report.deployed.push(id.clone());
                if let Err(e) = release(&mut held, &id, rows, state, &mut report, checkpoint) {
                    abort(installer, state, &mut report, &held, None, checkpoint)?;
                    return Err(e);
                }
            }
            Err(why) => {
                // Only what the GO names and this attempt installed is switched off: the timers held for
                // their service, and the failed unit itself when it triggers itself (a timer, socket, path).
                // A failure before the install changed nothing, so it leaves the rerun open.
                // The first-run failure is set before the cleanup is saved, so one checkpoint carries both and a
                // lost write can never leave the cleanup without the failure.
                if changed {
                    state.failed = Some(id.clone());
                }
                abort(
                    installer,
                    state,
                    &mut report,
                    &held,
                    changed.then_some(t),
                    checkpoint,
                )?;
                if changed && state.cleanup.is_empty() {
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
