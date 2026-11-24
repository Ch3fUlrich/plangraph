use super::ledger::{self, Ledger};
use super::live::{read_live, Entry, HostFacts, Live};
use super::report;
use super::systemd::fixture::Fixture;
use super::*;
use std::cell::Cell;

const MANIFEST: &str = r#"
[[unit]]
name = "pg-a"
files = ["pg-a.service", "pg-a.timer"]
hosts = ["keeper"]
first_run = "start pg-a.service"

[[unit]]
name = "pg-svc"
files = ["pg-svc.service"]
hosts = ["keeper"]
first_run = "journal shows a poll"

[[unit]]
name = "pg-tpl"
files = ["pg-tpl@.service"]
hosts = ["keeper"]
first_run = "enable an instance"

[[unit]]
name = "pg-w"
files = ["pg-w.service"]
hosts = ["worker"]
first_run = "x"

[[unit]]
name = "pg-off"
files = ["pg-off.service"]
not_deployed = "operator choice"

[[binary]]
package = "p"
bin = "tool"
hosts = ["all"]
first_run = "tool --version"
"#;

struct Facts {
    installed: bool,
    built: Vec<String>,
    source: Option<String>,
    merged: Option<i64>,
    now: Cell<i64>,
}

impl Facts {
    fn current() -> Self {
        Facts {
            installed: true,
            built: vec!["tree1".into()],
            source: Some("tree1".into()),
            merged: Some(1_000),
            now: Cell::new(1_000 + 100 * 3600),
        }
    }
}

impl HostFacts for Facts {
    fn binary_installed(&self, _: &str) -> bool {
        self.installed
    }
    fn built_trees(&self) -> Vec<String> {
        self.built.clone()
    }
    fn source_tree(&self) -> Option<String> {
        self.source.clone()
    }
    fn merged_at(&self, _: &[String]) -> Option<i64> {
        self.merged
    }
    fn now(&self) -> i64 {
        self.now.get()
    }
}

fn show_with(
    sysctl: &mut Fixture,
    unit: &str,
    (load, file, active, result): (&str, &str, &str, &str),
    (reload, started, status): (&str, &str, &str),
) {
    let props: Vec<String> = super::live::SHOW_PROPS
        .iter()
        .map(|p| format!("-p {p}"))
        .collect();
    let call = format!("--user show {unit} {}", props.join(" "));
    sysctl.answer(
        &call,
        0,
        &format!("LoadState={load}\nActiveState={active}\nUnitFileState={file}\nResult={result}\nNeedDaemonReload={reload}\nExecMainStartTimestamp={started}\nExecMainStatus={status}\n"),
    );
}

/// A unit that has run to success once (a service with a start time), no reload pending.
fn show(sysctl: &mut Fixture, unit: &str, load: &str, file: &str, active: &str, result: &str) {
    show_with(
        sysctl,
        unit,
        (load, file, active, result),
        ("no", "Mon 2026-11-23 10:00:00 UTC", "0"),
    );
}

fn roles(list: &[&str]) -> BTreeSet<String> {
    list.iter().map(|r| (*r).to_owned()).collect()
}

fn find<'a>(entries: &'a [Entry], name: &str) -> &'a Entry {
    entries
        .iter()
        .find(|e| e.name == name)
        .unwrap_or_else(|| panic!("no {name}"))
}

fn healthy(sysctl: &mut Fixture) {
    show(
        sysctl,
        "pg-a.timer",
        "loaded",
        "enabled",
        "active",
        "success",
    );
    show(
        sysctl,
        "pg-a.service",
        "loaded",
        "static",
        "inactive",
        "success",
    );
    show(
        sysctl,
        "pg-svc.service",
        "loaded",
        "enabled",
        "active",
        "success",
    );
    show(
        sysctl,
        "pg-tpl@.service",
        "loaded",
        "static",
        "inactive",
        "success",
    );
}

#[test]
fn healthy_units_and_binaries_are_live_and_other_roles_are_elsewhere() {
    let manifest = Manifest::parse(MANIFEST).unwrap();
    let mut sysctl = Fixture::default();
    healthy(&mut sysctl);
    let entries = read_live(&manifest, &roles(&["keeper"]), &sysctl, &Facts::current());
    assert_eq!(entries.len(), 6);
    assert!(entries.iter().all(|e| e.live.is_live()), "{entries:?}");
    assert!(matches!(find(&entries, "pg-w").live, Live::Elsewhere(_)));
    assert!(
        matches!(&find(&entries, "pg-off").live, Live::Elsewhere(r) if r.contains("operator choice"))
    );
    assert_eq!(ledger::counts(&entries), 0);
}

#[test]
fn a_unit_that_is_not_loaded_enabled_or_active_is_not_live_with_its_reason() {
    let manifest = Manifest::parse(MANIFEST).unwrap();
    let mut sysctl = Fixture::default();
    show(&mut sysctl, "pg-a.timer", "not-found", "", "inactive", "");
    show(
        &mut sysctl,
        "pg-svc.service",
        "loaded",
        "disabled",
        "inactive",
        "success",
    );
    show(
        &mut sysctl,
        "pg-tpl@.service",
        "loaded",
        "static",
        "inactive",
        "success",
    );
    let entries = read_live(&manifest, &roles(&["keeper"]), &sysctl, &Facts::current());
    assert_eq!(find(&entries, "pg-a").live, Live::NotInstalled);
    assert_eq!(
        find(&entries, "pg-svc").live,
        Live::NotEnabled("disabled".into())
    );
    assert_eq!(ledger::counts(&entries), 2);

    let mut sysctl = Fixture::default();
    healthy(&mut sysctl);
    show(
        &mut sysctl,
        "pg-a.timer",
        "loaded",
        "enabled",
        "inactive",
        "success",
    );
    let entries = read_live(&manifest, &roles(&["keeper"]), &sysctl, &Facts::current());
    assert_eq!(
        find(&entries, "pg-a").live,
        Live::NotActive("inactive".into())
    );
}

#[test]
fn an_active_timer_whose_service_failed_its_last_run_is_not_live() {
    let manifest = Manifest::parse(MANIFEST).unwrap();
    let mut sysctl = Fixture::default();
    healthy(&mut sysctl);
    show(
        &mut sysctl,
        "pg-a.service",
        "loaded",
        "static",
        "failed",
        "exit-code",
    );
    let entries = read_live(&manifest, &roles(&["keeper"]), &sysctl, &Facts::current());
    assert_eq!(
        find(&entries, "pg-a").live,
        Live::LastRunFailed("exit-code".into())
    );
}

#[test]
fn unreadable_systemd_counts_as_not_live() {
    let manifest = Manifest::parse(MANIFEST).unwrap();
    let sysctl = Fixture {
        unavailable: true,
        ..Fixture::default()
    };
    let entries = read_live(&manifest, &roles(&["keeper"]), &sysctl, &Facts::current());
    assert!(matches!(find(&entries, "pg-a").live, Live::Unreadable(_)));
    assert_eq!(ledger::counts(&entries), 3, "pg-a, pg-svc, pg-tpl");
}

#[test]
fn a_binary_is_missing_stale_or_live() {
    let manifest = Manifest::parse(MANIFEST).unwrap();
    let mut sysctl = Fixture::default();
    healthy(&mut sysctl);
    let missing = Facts {
        installed: false,
        ..Facts::current()
    };
    assert_eq!(
        find(
            &read_live(&manifest, &roles(&[]), &sysctl, &missing),
            "p/tool"
        )
        .live,
        Live::NotInstalled
    );
    let stale = Facts {
        built: vec!["old".into()],
        ..Facts::current()
    };
    assert!(matches!(
        find(
            &read_live(&manifest, &roles(&[]), &sysctl, &stale),
            "p/tool"
        )
        .live,
        Live::Stale(_)
    ));
    let unstamped = Facts {
        built: vec![],
        source: None,
        ..Facts::current()
    };
    assert!(matches!(
        find(
            &read_live(&manifest, &roles(&[]), &sysctl, &unstamped),
            "p/tool"
        )
        .live,
        Live::Stale(_)
    ));
}

#[test]
fn the_ledger_starts_at_the_merge_time_keeps_first_seen_and_drops_live_entries() {
    let manifest = Manifest::parse(MANIFEST).unwrap();
    let facts = Facts::current();
    let sysctl = Fixture::default(); // every unit answers with an empty show: not loaded
    let entries = read_live(&manifest, &roles(&["keeper"]), &sysctl, &facts);
    let first = ledger::update(&Ledger::default(), &entries, facts.now());
    assert_eq!(
        first.not_live_since["unit:pg-a"], 1_000,
        "age starts at the merge, not at the first run"
    );
    assert_eq!(
        ledger::age_hours(first.not_live_since["unit:pg-a"], facts.now()),
        100
    );
    // A later run with a newer merge time keeps the first-seen time.
    let later = Facts {
        merged: Some(50_000),
        now: Cell::new(1_000 + 200 * 3600),
        ..Facts::current()
    };
    let again = ledger::update(
        &first,
        &read_live(&manifest, &roles(&["keeper"]), &sysctl, &later),
        later.now(),
    );
    assert_eq!(again.not_live_since["unit:pg-a"], 1_000);
    // Once live, the entry leaves the ledger.
    let mut healthy_ctl = Fixture::default();
    healthy(&mut healthy_ctl);
    let live = read_live(&manifest, &roles(&["keeper"]), &healthy_ctl, &later);
    assert!(ledger::update(&again, &live, later.now())
        .not_live_since
        .is_empty());
}

#[test]
fn status_lines_show_state_age_and_the_first_run_check_and_the_count() {
    let manifest = Manifest::parse(MANIFEST).unwrap();
    let facts = Facts::current();
    let entries = read_live(&manifest, &roles(&["keeper"]), &Fixture::default(), &facts);
    let ledger = ledger::update(&Ledger::default(), &entries, facts.now());
    let lines = report::lines(&entries, &ledger, facts.now());
    assert!(
        lines.contains(
            &"deploy-entry\tunit\tpg-a\tnot-installed\t100h\tstart pg-a.service".to_owned()
        ),
        "{lines:#?}"
    );
    assert!(
        lines
            .iter()
            .any(|l| l.starts_with("deploy-entry\tunit\tpg-w\telsewhere(roles worker)\t-\t")),
        "{lines:#?}"
    );
    assert_eq!(lines.last().unwrap(), "deploy-not-live\t3");
}

#[test]
fn status_writes_the_ledger_file_the_go_check_reads() {
    let dir = tempfile::tempdir().unwrap();
    std::fs::create_dir_all(dir.path().join("deploy")).unwrap();
    std::fs::write(dir.path().join("deploy/manifest.toml"), MANIFEST).unwrap();
    let path = dir.path().join("state/deploy-live/h.json");
    let facts = Facts::current();
    let lines = report::status(
        dir.path(),
        &roles(&["keeper"]),
        &Fixture::default(),
        &facts,
        Some(&path),
    )
    .unwrap();
    assert!(lines.last().unwrap().starts_with("deploy-not-live\t"));
    let saved = ledger::load(&path);
    assert_eq!(saved.written_at, facts.now());
    assert!(
        saved.not_live_since.contains_key("unit:pg-a")
            && !saved.not_live_since.contains_key("binary:p/tool")
    );
}

#[test]
fn the_digest_line_counts_not_live_entries_across_hosts_and_names_the_oldest() {
    let dir = tempfile::tempdir().unwrap();
    let now = 100 * 3600;
    assert!(
        report::digest_line(dir.path(), now).contains("unknown"),
        "no ledger is not zero"
    );
    let mut a = Ledger {
        written_at: now,
        ..Ledger::default()
    };
    a.not_live_since
        .insert("unit:pg-feeder".into(), now - 30 * 3600);
    a.not_live_since
        .insert("unit:pg-throughput".into(), now - 2 * 3600);
    ledger::save(&dir.path().join("host-a.json"), &a).unwrap();
    let mut b = Ledger {
        written_at: now,
        ..Ledger::default()
    };
    b.not_live_since
        .insert("binary:p/tool".into(), now - 50 * 3600);
    ledger::save(&dir.path().join("host-b.json"), &b).unwrap();
    assert_eq!(
        report::digest_line(dir.path(), now),
        "deploy-not-live 3 oldest host-b:binary:p/tool 50h (2 hosts reporting)"
    );
}

#[test]
fn an_active_timer_is_not_live_while_its_service_is_missing_or_has_never_run() {
    let manifest = Manifest::parse(MANIFEST).unwrap();
    let live = |service: &dyn Fn(&mut Fixture)| {
        let mut sysctl = Fixture::default();
        healthy(&mut sysctl);
        service(&mut sysctl);
        find(
            &read_live(&manifest, &roles(&["keeper"]), &sysctl, &Facts::current()),
            "pg-a",
        )
        .live
        .clone()
    };
    assert!(matches!(
        live(&|s| show(s, "pg-a.service", "not-found", "", "inactive", "")),
        Live::NeverRun(why) if why.contains("not loaded")
    ));
    assert!(matches!(
        live(&|s| show_with(s, "pg-a.service", ("loaded", "static", "inactive", "success"), ("no", "", ""))),
        Live::NeverRun(why) if why.contains("has not run")
    ));
    assert_eq!(
        live(&|s| show_with(
            s,
            "pg-a.service",
            ("loaded", "static", "inactive", "success"),
            ("no", "Mon", "3")
        )),
        Live::LastRunFailed("3".into()),
        "a non-zero exit status of the last run is a failure even when Result is success"
    );
    assert_eq!(
        live(&|s| show_with(
            s,
            "pg-a.service",
            ("loaded", "static", "inactive", "success"),
            ("no", "Mon", "0")
        )),
        Live::Live
    );
}

#[test]
fn a_unit_file_changed_on_disk_but_not_reloaded_is_not_live() {
    let manifest = Manifest::parse(MANIFEST).unwrap();
    let mut sysctl = Fixture::default();
    healthy(&mut sysctl);
    show_with(
        &mut sysctl,
        "pg-a.timer",
        ("loaded", "enabled", "active", "success"),
        ("yes", "Mon", "0"),
    );
    let entries = read_live(&manifest, &roles(&["keeper"]), &sysctl, &Facts::current());
    assert_eq!(find(&entries, "pg-a").live, Live::NeedsReload);
}
