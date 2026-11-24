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
    /// The hash of the file the consumer runs; `None` is not installed.
    installed: Option<String>,
    /// `bin -> (release stamp, built artifact hash)`.
    records: std::collections::BTreeMap<String, (String, String)>,
    source: Option<String>,
    merged: Option<i64>,
    /// `unit file -> when it was written into the unit directory`.
    written: std::collections::BTreeMap<String, i64>,
    now: Cell<i64>,
}

impl Facts {
    fn current() -> Self {
        Facts {
            installed: Some("sha1".into()),
            records: [("tool".to_owned(), ("tree1".to_owned(), "sha1".to_owned()))].into(),
            source: Some("tree1".into()),
            merged: Some(1_000),
            written: Default::default(),
            now: Cell::new(1_000 + 100 * 3600),
        }
    }
}

impl HostFacts for Facts {
    fn installed_sha(&self, _: &super::BinaryEntry) -> Option<String> {
        self.installed.clone()
    }
    fn artifact_record(&self, bin: &str) -> Option<(String, String)> {
        self.records.get(bin).cloned()
    }
    fn source_tree(&self) -> Option<String> {
        self.source.clone()
    }
    fn merged_at(&self, _: &[String]) -> Option<i64> {
        self.merged
    }
    fn unit_file_changed_at(&self, _: &super::UnitEntry, file: &str) -> Option<i64> {
        self.written.get(file).copied()
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
    show_at(
        sysctl,
        unit,
        (load, file, active, result),
        (reload, started, status),
        "@1000000",
    );
}

fn show_at(
    sysctl: &mut Fixture,
    unit: &str,
    (load, file, active, result): (&str, &str, &str, &str),
    (reload, started, status): (&str, &str, &str),
    entered: &str,
) {
    let props: Vec<String> = super::live::SHOW_PROPS
        .iter()
        .map(|p| format!("-p {p}"))
        .collect();
    let call = format!("--user show --timestamp=unix {unit} {}", props.join(" "));
    sysctl.answer(
        &call,
        0,
        &format!("LoadState={load}\nActiveState={active}\nUnitFileState={file}\nResult={result}\nNeedDaemonReload={reload}\nExecMainStartTimestamp={started}\nExecMainStatus={status}\nActiveEnterTimestamp={entered}\n"),
    );
}

/// A unit that has run to success once (a service with a start time), no reload pending.
fn show(sysctl: &mut Fixture, unit: &str, load: &str, file: &str, active: &str, result: &str) {
    show_with(
        sysctl,
        unit,
        (load, file, active, result),
        ("no", "@1000000", "0"),
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
fn a_binary_is_live_only_as_the_artifact_built_for_this_release_at_the_path_its_consumer_runs() {
    let manifest = Manifest::parse(MANIFEST).unwrap();
    let mut sysctl = Fixture::default();
    healthy(&mut sysctl);
    let state = |facts: &Facts| {
        find(&read_live(&manifest, &roles(&[]), &sysctl, facts), "p/tool")
            .live
            .clone()
    };
    assert_eq!(state(&Facts::current()), Live::Live);
    let missing = Facts {
        installed: None,
        ..Facts::current()
    };
    assert_eq!(state(&missing), Live::NotInstalled);
    let mut old_release = Facts::current();
    old_release
        .records
        .insert("tool".into(), ("old".into(), "sha1".into()));
    assert!(
        matches!(state(&old_release), Live::Stale(why) if why.contains("built from release old"))
    );
    let mut other_file = Facts::current();
    other_file.installed = Some("sha-of-an-older-build".into());
    assert!(
        matches!(state(&other_file), Live::Stale(why) if why.contains("not the artifact built"))
    );
    let mut no_record = Facts::current();
    no_record.records.clear();
    assert!(
        matches!(state(&no_record), Live::Stale(why) if why.contains("no release record")),
        "a current host-wide stamp does not certify a binary the build never recorded"
    );
    let unstamped = Facts {
        source: None,
        ..Facts::current()
    };
    assert!(matches!(state(&unstamped), Live::Stale(_)));
}

#[test]
fn a_unit_that_runs_an_older_revision_of_its_file_is_not_live() {
    let manifest = Manifest::parse(MANIFEST).unwrap();
    let run = |written: i64, timer_entered: &str, service_started: &str| {
        let mut sysctl = Fixture::default();
        healthy(&mut sysctl);
        show_at(
            &mut sysctl,
            "pg-a.timer",
            ("loaded", "enabled", "active", "success"),
            ("no", "@0", "0"),
            timer_entered,
        );
        show_with(
            &mut sysctl,
            "pg-a.service",
            ("loaded", "static", "inactive", "success"),
            ("no", service_started, "0"),
        );
        let mut facts = Facts::current();
        facts.written = [
            ("pg-a.timer".to_owned(), written),
            ("pg-a.service".to_owned(), written),
        ]
        .into();
        find(
            &read_live(&manifest, &roles(&["keeper"]), &sysctl, &facts),
            "pg-a",
        )
        .live
        .clone()
    };
    assert_eq!(run(500, "@600", "@700"), Live::Live);
    assert!(
        matches!(run(500, "@400", "@700"), Live::Stale(why) if why.contains("older than its installed file"))
    );
    assert!(
        matches!(run(500, "@600", "@400"), Live::NeverRun(why) if why.contains("before its installed file"))
    );
    assert!(
        matches!(run(500, "n/a", "@700"), Live::Stale(_)),
        "no start time of the timer: unknown, not live"
    );
}

#[test]
fn a_service_that_needs_a_reload_keeps_its_active_timer_not_live() {
    let manifest = Manifest::parse(MANIFEST).unwrap();
    let mut sysctl = Fixture::default();
    healthy(&mut sysctl);
    show_with(
        &mut sysctl,
        "pg-a.service",
        ("loaded", "static", "inactive", "success"),
        ("yes", "@1000000", "0"),
    );
    let entries = read_live(&manifest, &roles(&["keeper"]), &sysctl, &Facts::current());
    assert_eq!(find(&entries, "pg-a").live, Live::NeedsReload);
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
        report::digest_line(dir.path(), None, now).contains("unknown"),
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
        report::digest_line(dir.path(), None, now),
        "deploy-not-live 3 oldest host-b:binary:p/tool 50h (2 hosts reporting, no hosts registry: every ledger file counted)"
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
            ("no", "@1000000", "3")
        )),
        Live::LastRunFailed("3".into()),
        "a non-zero exit status of the last run is a failure even when Result is success"
    );
    assert_eq!(
        live(&|s| show_with(
            s,
            "pg-a.service",
            ("loaded", "static", "inactive", "success"),
            ("no", "@1000000", "0")
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
        ("yes", "@1000000", "0"),
    );
    let entries = read_live(&manifest, &roles(&["keeper"]), &sysctl, &Facts::current());
    assert_eq!(find(&entries, "pg-a").live, Live::NeedsReload);
}

#[test]
fn a_unit_is_dated_from_its_landing_commit_not_the_topic_commit() {
    use std::process::Command;
    let dir = tempfile::tempdir().unwrap();
    let repo = dir.path();
    let git = |args: &[&str], date: &str| {
        let out = Command::new("git")
            .arg("-C")
            .arg(repo)
            .args(["-c", "user.name=F", "-c", "user.email=f@example.invalid"])
            .args(args)
            .env("GIT_AUTHOR_DATE", date)
            .env("GIT_COMMITTER_DATE", date)
            .output()
            .unwrap();
        assert!(out.status.success(), "{out:?}");
    };
    git(&["init", "-q", "-b", "main"], "@1000000000 +0000");
    git(
        &["commit", "-q", "--allow-empty", "-m", "base"],
        "@1000000000 +0000",
    );
    git(&["checkout", "-q", "-b", "topic"], "@1000000000 +0000");
    std::fs::create_dir_all(repo.join("deploy/systemd")).unwrap();
    std::fs::write(repo.join("deploy/systemd/u.service"), "[Unit]\n").unwrap();
    git(&["add", "-A"], "@1000000100 +0000");
    git(&["commit", "-q", "-m", "topic"], "@1000000100 +0000");
    git(&["checkout", "-q", "main"], "@1000000200 +0000");
    git(
        &["merge", "-q", "--no-ff", "-m", "land", "topic"],
        "@1000090000 +0000",
    );
    let facts = report::LocalFacts {
        repo: repo.to_path_buf(),
        bin_dir: repo.join("bin"),
        state_dir: repo.join("state"),
        unit_dir: repo.join("units"),
    };
    assert_eq!(
        facts.merged_at(&["deploy/systemd/u.service".to_owned()]),
        Some(1_000_090_000),
        "the merge date, not the topic commit's"
    );
}

const REGISTRY: &str = r#"{"version":1,"self":"center","hosts":[
 {"id":"center","aliases":["coding-vm"],"transport":"local"},
 {"id":"dell","aliases":[],"transport":"ssh","ssh_target":"d"},
 {"id":"desk","aliases":[],"transport":"ssh","ssh_target":"k"}]}"#;

#[test]
fn the_digest_counts_a_host_once_by_id_or_alias_and_names_missing_and_stale_hosts() {
    let dir = tempfile::tempdir().unwrap();
    let now = 100 * 3600;
    let mut with_entry = Ledger {
        written_at: now,
        ..Ledger::default()
    };
    with_entry
        .not_live_since
        .insert("unit:pg-feeder".into(), now - 30 * 3600);
    // The same host under its id and its alias: one count, not two.
    ledger::save(&dir.path().join("center.json"), &with_entry).unwrap();
    ledger::save(&dir.path().join("coding-vm.json"), &with_entry).unwrap();
    // A stale clean ledger does not count as zero; a host with no ledger is named.
    ledger::save(
        &dir.path().join("dell.json"),
        &Ledger {
            written_at: now - 10 * 3600,
            ..Ledger::default()
        },
    )
    .unwrap();
    let line = report::digest_line(dir.path(), Some(REGISTRY), now);
    assert_eq!(
        line,
        "deploy-not-live unknown: 1 counted oldest center:unit:pg-feeder 30h (1 hosts reporting, missing: desk, stale: dell)"
    );
    ledger::save(
        &dir.path().join("dell.json"),
        &Ledger {
            written_at: now,
            ..Ledger::default()
        },
    )
    .unwrap();
    ledger::save(
        &dir.path().join("desk.json"),
        &Ledger {
            written_at: now,
            ..Ledger::default()
        },
    )
    .unwrap();
    assert_eq!(
        report::digest_line(dir.path(), Some(REGISTRY), now),
        "deploy-not-live 1 oldest center:unit:pg-feeder 30h (3 hosts reporting)"
    );
}
