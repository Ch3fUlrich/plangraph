use super::deploy_post::*;
use std::cell::RefCell;
use std::collections::{BTreeMap, BTreeSet};

pub(super) fn set(v: &[&str]) -> BTreeSet<String> {
    v.iter().map(|s| s.to_string()).collect()
}

pub(super) fn target(family: &str, host: &str, items: &[&str]) -> RunTarget {
    RunTarget {
        family: family.into(),
        host: host.into(),
        items: set(items),
    }
}

pub(super) fn active() -> RunRead {
    RunRead {
        is_active: "active".into(),
        result: None,
        exec_main_exit_timestamp: None,
        oneshot: false,
        invocation: None,
    }
}

pub(super) fn oneshot_ok() -> RunRead {
    RunRead {
        is_active: "inactive".into(),
        result: Some("success".into()),
        exec_main_exit_timestamp: Some("2026-11-24T01:00:00Z".into()),
        oneshot: true,
        invocation: Some("inv-1".into()),
    }
}

/// The landed revision of the test gate.
pub(super) const REV: &str = "rev-1";

/// The fixture oneshot exits at this instant; the test clock starts there.
pub(super) const T0: i64 = 1_795_482_000_000_000; // 2026-11-24T01:00:00Z, in microseconds

thread_local! {
    static NOW: std::cell::Cell<i64> = const { std::cell::Cell::new(T0) };
}

pub(super) fn clock() -> i64 {
    NOW.with(|n| n.get())
}

#[derive(Default)]
pub(super) struct Host {
    pub(super) runs: BTreeMap<String, RunRead>,
    pub(super) calls: RefCell<Vec<String>>,
    /// Seconds the test clock advances while an install runs.
    pub(super) install_takes: i64,
    /// Targets whose read fails.
    pub(super) unreadable: BTreeSet<String>,
    /// What a oneshot shows before its install (it is absent before it unless listed here).
    pub(super) before: BTreeMap<String, RunRead>,
    /// Oneshots whose read before the install fails (a transient error, not an absent unit).
    pub(super) before_fails: BTreeSet<String>,
    pub(super) installed: RefCell<BTreeSet<String>>,
    /// The invocation an install reports starting, when it is not the one the run shows.
    pub(super) started: BTreeMap<String, String>,
    /// Installs that print no invocation.
    pub(super) silent_install: BTreeSet<String>,
    /// Units whose disable fails (the test heals them by removing the entry).
    pub(super) disable_fails: RefCell<BTreeSet<String>>,
    /// Installs that fail.
    pub(super) install_fails: BTreeSet<String>,
}

impl Installer for Host {
    fn install(&self, host: &str, family: &str) -> Result<Option<String>, String> {
        self.calls
            .borrow_mut()
            .push(format!("install {family}@{host}"));
        if self.install_fails.contains(family) {
            return Err("no space".into());
        }
        NOW.with(|n| n.set(n.get() + self.install_takes * 1_000_000));
        self.installed.borrow_mut().insert(family.to_owned());
        if self.silent_install.contains(family) {
            return Ok(None);
        }
        Ok(self
            .started
            .get(family)
            .cloned()
            .or_else(|| self.runs.get(family).and_then(|r| r.invocation.clone())))
    }
    fn read_run(&self, host: &str, family: &str) -> Result<RunRead, String> {
        self.calls
            .borrow_mut()
            .push(format!("read {family}@{host}"));
        if self.unreadable.contains(family) {
            return Err("unreadable".into());
        }
        let started_unit = self
            .runs
            .get(family)
            .is_some_and(|r| r.invocation.is_some());
        if started_unit && !self.installed.borrow().contains(family) {
            if self.before_fails.contains(family) {
                return Err("transient".into());
            }
            return Ok(self.before.get(family).cloned().unwrap_or(RunRead {
                is_active: ABSENT.into(),
                result: None,
                exec_main_exit_timestamp: None,
                oneshot: false,
                invocation: None,
            }));
        }
        self.runs
            .get(family)
            .cloned()
            .ok_or_else(|| "no run".into())
    }
    fn disable(&self, host: &str, unit: &str) -> Result<(), String> {
        self.calls
            .borrow_mut()
            .push(format!("disable {unit}@{host}"));
        if self.disable_fails.borrow().contains(unit) {
            return Err("host unreachable".into());
        }
        Ok(())
    }
}

#[derive(Default)]
pub(super) struct Rows(pub(super) RefCell<Vec<DeployRow>>);

impl RowSink for Rows {
    fn write(&self, row: &DeployRow) -> Result<(), String> {
        self.0.borrow_mut().push(row.clone());
        Ok(())
    }
}

/// Landed head at epoch 1e9; the test clock is at [`T0`], the instant the oneshot fixture exited.
pub(super) fn gate() -> Gate {
    NOW.with(|n| n.set(T0));
    Gate {
        revision: REV.into(),
        fresh_after: 1_000_000_000_000_000,
        clock,
    }
}

pub(super) fn run(
    targets: &[RunTarget],
    host: &Host,
    rows: &Rows,
    state: &mut PostState,
) -> Result<PostReport, String> {
    apply(targets, &gate(), host, rows, state, &mut |_| Ok(()))
}

/// A host whose units show these runs.
pub(super) fn host_of(units: &[(&str, RunRead)]) -> Host {
    let mut host = Host::default();
    for (name, run) in units {
        host.runs.insert((*name).into(), run.clone());
    }
    host
}

/// One run over a fresh state: the report, the rows written and the state.
pub(super) fn go(host: &Host, targets: &[RunTarget]) -> (PostReport, Vec<DeployRow>, PostState) {
    let (rows, mut state) = (Rows::default(), PostState::default());
    let report = run(targets, host, &rows, &mut state).unwrap();
    (report, rows.0.into_inner(), state)
}

/// `(item, unit)` of each row.
pub(super) fn pairs(rows: &[DeployRow]) -> Vec<(&str, &str)> {
    rows.iter()
        .map(|r| (r.key.as_str(), r.unit.as_str()))
        .collect()
}

/// The disable commands a host received.
pub(super) fn disables(host: &Host) -> Vec<String> {
    let calls = host.calls.borrow();
    calls
        .iter()
        .filter(|c| c.starts_with("disable"))
        .cloned()
        .collect()
}

pub(super) fn with_result(mut run: RunRead, result: &str) -> RunRead {
    run.result = Some(result.into());
    run
}

pub(super) fn exited(at: &str) -> RunRead {
    RunRead {
        exec_main_exit_timestamp: Some(at.into()),
        ..oneshot_ok()
    }
}

#[test]
fn a_good_first_run_writes_one_deploy_row_per_item_and_the_timer_row_beside_it() {
    let host = host_of(&[("a.service", oneshot_ok()), ("a.timer", active())]);
    let (r, rows, state) = go(
        &host,
        &[target("a.service", "alpha", &["demo:A", "demo:B"])],
    );
    assert_eq!(r.deployed, ["a.service@alpha"]);
    assert_eq!(
        pairs(&rows),
        [
            ("demo:A", "a.timer"),
            ("demo:A", "a.service"),
            ("demo:B", "a.timer"),
            ("demo:B", "a.service")
        ]
    );
    assert_eq!(
        (rows[1].result.as_deref(), rows[1].kind.as_deref()),
        (Some("success"), Some("oneshot"))
    );
    assert!(state.done.contains("a.service@alpha"));
}

#[test]
fn a_failed_first_run_writes_no_row_takes_the_held_timer_down_and_stops_the_rest() {
    let host = host_of(&[
        ("a.service", with_result(oneshot_ok(), "exit-code")),
        ("a.timer", active()),
        ("b.service", oneshot_ok()),
        ("b.timer", active()),
    ]);
    let targets = [
        target("a.timer", "alpha", &["demo:A"]),
        target("a.service", "alpha", &["demo:A"]),
        target("b.service", "alpha", &["demo:B"]),
    ];
    let (r, rows, state) = go(&host, &targets);
    assert!(rows.is_empty());
    assert_eq!(r.failed.as_ref().unwrap().0, "a.service@alpha");
    assert_eq!(r.not_attempted, ["b.service@alpha"]);
    assert_eq!(disables(&host), ["disable a.timer@alpha"]);
    assert!(!host.calls.borrow().iter().any(|c| c.contains("b.service")));
    assert_eq!(state.failed.as_deref(), Some("a.service@alpha"));
}

#[test]
fn an_install_failure_is_a_failed_first_run() {
    let mut host = host_of(&[("a.timer", active())]);
    host.install_fails.insert("a.timer".into());
    let (r, rows, _) = go(&host, &[target("a.timer", "alpha", &["demo:A"])]);
    assert!(r.failed.unwrap().1.contains("no space"));
    assert!(rows.is_empty());
}

#[test]
fn a_rerun_skips_finished_targets_and_a_recorded_failure_blocks_it() {
    let host = host_of(&[
        ("a.service", oneshot_ok()),
        ("b.service", oneshot_ok()),
        ("b.timer", active()),
    ]);
    let targets = [
        target("a.service", "alpha", &["demo:A"]),
        target("b.service", "alpha", &["demo:B"]),
    ];
    let mut state = PostState {
        plan: plan_id(&targets, REV),
        done: ["a.service@alpha".to_string()].into(),
        ..PostState::default()
    };
    let mut saved = 0;
    let r = apply(
        &targets,
        &gate(),
        &host,
        &Rows::default(),
        &mut state,
        &mut |_| {
            saved += 1;
            Ok(())
        },
    )
    .unwrap();
    assert_eq!(r.skipped_done, ["a.service@alpha"]);
    assert_eq!(r.deployed, ["b.service@alpha"]);
    assert!(!host.calls.borrow().iter().any(|c| c.contains("a.service")));
    assert_eq!(saved, 1, "state is saved after each finished target");
    state.failed = Some("c.service@alpha".into());
    assert!(run(&targets, &host, &Rows::default(), &mut state).is_err());
}

#[test]
fn a_good_oneshot_needs_its_timer_active_and_a_shipped_timer_is_recorded_after_its_service() {
    let mut off = active();
    off.is_active = "inactive".into();
    let host = host_of(&[("a.service", oneshot_ok()), ("a.timer", off)]);
    let (r, rows, _) = go(&host, &[target("a.service", "alpha", &["demo:A"])]);
    assert!(r.failed.unwrap().1.contains("paired timer a.timer"));
    assert!(rows.is_empty() && disables(&host).is_empty());
    // Shipped for the same item: installed first, recorded after the service, no row twice.
    let host = host_of(&[("a.service", oneshot_ok()), ("a.timer", active())]);
    let both = [
        target("a.service", "alpha", &["demo:A"]),
        target("a.timer", "alpha", &["demo:A"]),
    ];
    let (r, rows, _) = go(&host, &both);
    assert_eq!(r.deployed, ["a.service@alpha", "a.timer@alpha"]);
    let calls = host.calls.borrow();
    assert!(
        calls.iter().position(|c| c == "install a.timer@alpha")
            < calls.iter().position(|c| c == "install a.service@alpha")
    );
    assert_eq!(
        pairs(&rows),
        [("demo:A", "a.service"), ("demo:A", "a.timer")]
    );
}

#[test]
fn a_timer_shipped_for_other_items_still_gets_a_row_for_every_service_item() {
    let host = host_of(&[("a.service", oneshot_ok()), ("a.timer", active())]);
    let targets = [
        target("a.service", "alpha", &["demo:A"]),
        target("a.timer", "alpha", &["demo:B"]),
    ];
    let (r, rows, state) = go(&host, &targets);
    assert_eq!(r.deployed, ["a.service@alpha", "a.timer@alpha"]);
    let mut got = pairs(&rows);
    got.sort();
    assert_eq!(
        got,
        [
            ("demo:A", "a.service"),
            ("demo:A", "a.timer"),
            ("demo:B", "a.timer")
        ]
    );
    assert!(state.done.contains("a.timer@alpha") && state.done.contains("a.service@alpha"));
}

#[test]
fn a_shipped_timer_leaves_no_evidence_when_its_service_fails() {
    let host = host_of(&[
        ("a.service", with_result(oneshot_ok(), "exit-code")),
        ("a.timer", active()),
    ]);
    let targets = [
        target("a.service", "alpha", &["demo:A"]),
        target("a.timer", "alpha", &["demo:B"]),
    ];
    let (r, rows, state) = go(&host, &targets);
    assert_eq!(r.failed.unwrap().0, "a.service@alpha");
    assert!(rows.is_empty() && state.done.is_empty());
    assert_eq!(disables(&host), ["disable a.timer@alpha"]);
}

#[test]
fn an_unparsable_stale_or_future_exit_time_is_a_failed_first_run() {
    for bad in [
        "t",
        "",
        "2001-01-01T00:00:00Z",        // before the landed head
        "2099-01-01T00:00:00Z",        // after "now"
        "2026-11-24T01:00:03Z",        // three seconds ahead: the keeper rejects it
        "2026-11-24T01:00:00.9Z",      // a fraction ahead: the keeper keeps the fraction
        "Mon 2001-09-09 01:46:39 UTC", // stale, systemd form
    ] {
        let host = host_of(&[("a.service", exited(bad)), ("a.timer", active())]);
        let (r, rows, _) = go(&host, &[target("a.service", "alpha", &["demo:A"])]);
        assert!(r.failed.is_some() && rows.is_empty(), "{bad:?}");
    }
    let host = host_of(&[
        ("a.service", exited("Tue 2026-11-24 01:00:00 UTC")),
        ("a.timer", active()),
    ]);
    assert_eq!(
        go(&host, &[target("a.service", "alpha", &["demo:A"])])
            .0
            .deployed,
        ["a.service@alpha"]
    );
}

#[test]
fn exit_times_keep_their_fraction_and_are_written_as_rfc_3339() {
    assert_eq!(exit_micros("2026-11-24T01:00:00Z"), Some(T0));
    assert_eq!(exit_micros("Tue 2026-11-24 01:00:00 UTC"), Some(T0));
    assert_eq!(exit_micros("1970-01-01T00:00:10.25Z"), Some(10_250_000));
    assert_eq!(
        exit_micros("Tue 2026-11-24 01:00:00.5 UTC"),
        Some(T0 + 500_000)
    );
    assert_eq!(exit_micros("t"), None);
    assert_eq!(
        exit_rfc3339("Tue 2026-11-24 01:00:00.5 UTC").as_deref(),
        Some("2026-11-24T01:00:00.500Z")
    );
    assert_eq!(
        exit_rfc3339("Tue 2026-11-24 01:00:00 UTC").as_deref(),
        Some("2026-11-24T01:00:00Z")
    );
    let host = host_of(&[
        ("a.service", exited("Tue 2026-11-24 01:00:00 UTC")),
        ("a.timer", active()),
    ]);
    let (_, rows, _) = go(&host, &[target("a.service", "alpha", &["demo:A"])]);
    let service = rows.iter().find(|r| r.unit == "a.service").unwrap();
    assert_eq!(
        service.exec_main_exit_timestamp.as_deref(),
        Some("2026-11-24T01:00:00Z")
    );
}

#[test]
fn a_slow_install_is_not_future_dated_and_a_result_from_before_it_is_not_its_first_run() {
    let mut host = host_of(&[
        ("a.service", exited("2026-11-24T01:00:02Z")),
        ("a.timer", active()),
    ]);
    host.install_takes = 2;
    let targets = [target("a.service", "alpha", &["demo:A"])];
    assert_eq!(go(&host, &targets).0.deployed, ["a.service@alpha"]);
    // The unit already showed this exit time before the install: the install did not run it again.
    let mut host = host_of(&[
        ("a.service", exited("2026-11-24T00:59:00Z")),
        ("a.timer", active()),
    ]);
    host.before
        .insert("a.service".into(), host.runs["a.service"].clone());
    let (r, rows, _) = go(&host, &targets);
    assert!(r.failed.is_some() && rows.is_empty());
    // A later exit time is a new run, even a minute before the clock: no clock skew is needed to tell.
    let mut host = host_of(&[
        ("a.service", exited("2026-11-24T00:59:30Z")),
        ("a.timer", active()),
    ]);
    let old = RunRead {
        invocation: Some("inv-0".into()),
        ..exited("2026-11-24T00:59:00Z")
    };
    host.before.insert("a.service".into(), old);
    assert_eq!(go(&host, &targets).0.deployed, ["a.service@alpha"]);
}

#[test]
fn only_the_invocation_the_install_started_is_proof_and_a_failed_baseline_read_installs_nothing() {
    let targets = [target("a.service", "alpha", &["demo:A"])];
    // The timer ran the service between the baseline read and the install.
    let mut host = host_of(&[
        ("a.service", exited("2026-11-24T00:59:30Z")),
        ("a.timer", active()),
    ]);
    host.runs.get_mut("a.service").unwrap().invocation = Some("timer-run".into());
    host.started
        .insert("a.service".into(), "install-run".into());
    let (r, rows, _) = go(&host, &targets);
    assert!(
        r.failed
            .unwrap()
            .1
            .contains("invocation=timer-run, started=install-run")
            && rows.is_empty()
    );
    // An install that reports no invocation proves nothing either.
    let mut host = host_of(&[
        ("a.service", exited("2026-11-24T00:59:30Z")),
        ("a.timer", active()),
    ]);
    host.silent_install.insert("a.service".into());
    let (r, rows, _) = go(&host, &targets);
    assert!(r.failed.is_some() && rows.is_empty());
    // Without a baseline nothing proves the run is new: the target fails before it is installed.
    let mut host = host_of(&[
        ("a.service", exited("2026-11-24T00:59:00Z")),
        ("a.timer", active()),
    ]);
    host.before_fails.insert("a.service".into());
    let (r, _, _) = go(&host, &targets);
    assert!(r.failed.unwrap().1.contains("read before install"));
    assert!(!host.installed.borrow().contains("a.service"));
}
