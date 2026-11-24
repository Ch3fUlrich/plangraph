//! `land-train deploy-guard`: the landing step's own deploy check, with its refresh through a fake runner.
use super::deploy_guard::run_with;
use crate::deploy_manifest::fleet::{FleetHost, Remote};
use crate::deploy_manifest::ledger::{self, Ledger};
use crate::deploy_manifest::systemd::Out;
use std::collections::BTreeMap;
use std::sync::Mutex;

const REGISTRY: &str = r#"{"version":1,"self":"center","hosts":[
 {"id":"center","aliases":[],"transport":"local"},
 {"id":"dell","aliases":[],"transport":"ssh","ssh_target":"dell-ssh"}]}"#;

fn now() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs() as i64
}

/// Answers a `--check --machine` run by writing nothing itself: the ledgers a host "reports" are the ones
/// the test put in the directory, and the runner records that it was asked.
struct Asked(Mutex<Vec<String>>);

impl Remote for Asked {
    fn run(&self, host: &FleetHost, _: &[String]) -> Result<Out, String> {
        self.0.lock().unwrap().push(host.id.clone());
        Ok(Out {
            code: 0,
            stdout: "pending\t0\n".into(),
            stderr: String::new(),
        })
    }
}

fn flags(dir: &std::path::Path, refresh: bool) -> BTreeMap<String, String> {
    std::fs::write(dir.join("hosts.json"), REGISTRY).unwrap();
    let mut flags = BTreeMap::from([
        (
            "deploy-ledgers".to_owned(),
            dir.join("ledgers").display().to_string(),
        ),
        (
            "hosts".to_owned(),
            dir.join("hosts.json").display().to_string(),
        ),
    ]);
    if refresh {
        flags.insert("refresh".into(), "yes".into());
    }
    flags
}

fn write(dir: &std::path::Path, host: &str, hours_not_live: Option<i64>) {
    let mut l = Ledger {
        written_at: now(),
        ..Ledger::default()
    };
    if let Some(h) = hours_not_live {
        l.not_live_since
            .insert("unit:pg-feeder".into(), now() - h * 3600 - 60);
        l.state
            .insert("unit:pg-feeder".into(), "not-installed".into());
    }
    ledger::save(&dir.join("ledgers").join(format!("{host}.json")), &l).unwrap();
}

#[test]
fn the_refresh_asks_every_registry_host_and_a_complete_fleet_passes() {
    let dir = tempfile::tempdir().unwrap();
    write(dir.path(), "center", None);
    write(dir.path(), "dell", Some(2));
    let asked = Asked(Mutex::new(Vec::new()));
    let lines = run_with(&flags(dir.path(), true), &asked).unwrap();
    let mut ids = asked.0.lock().unwrap().clone();
    ids.sort();
    assert_eq!(ids, ["center", "dell"]);
    assert!(
        lines.last().unwrap().starts_with("DEPLOY-LIVE"),
        "{lines:?}"
    );
}

#[test]
fn an_overdue_entry_on_any_host_prevents_landing() {
    let dir = tempfile::tempdir().unwrap();
    write(dir.path(), "center", None);
    write(dir.path(), "dell", Some(30));
    let refused = run_with(&flags(dir.path(), false), &Asked(Mutex::new(Vec::new()))).unwrap_err();
    assert!(
        refused.contains("deploy_not_live") && refused.contains("dell") && refused.contains("30h"),
        "{refused}"
    );
}

#[test]
fn a_host_that_never_reported_prevents_landing_and_no_registry_prevents_it_too() {
    let dir = tempfile::tempdir().unwrap();
    write(dir.path(), "center", None);
    let refused = run_with(&flags(dir.path(), false), &Asked(Mutex::new(Vec::new()))).unwrap_err();
    assert!(
        refused.contains("deploy_ledger_missing") && refused.contains("dell"),
        "{refused}"
    );
    let mut none = flags(dir.path(), false);
    none.insert(
        "hosts".into(),
        dir.path().join("absent.json").display().to_string(),
    );
    let refused = run_with(&none, &Asked(Mutex::new(Vec::new()))).unwrap_err();
    assert!(refused.contains("deploy_hosts_unknown"), "{refused}");
}

#[test]
fn a_refresh_without_a_registry_is_an_error_not_a_pass() {
    let dir = tempfile::tempdir().unwrap();
    let mut f = flags(dir.path(), true);
    f.insert(
        "hosts".into(),
        dir.path().join("absent.json").display().to_string(),
    );
    assert!(run_with(&f, &Asked(Mutex::new(Vec::new())))
        .unwrap_err()
        .contains("registry"));
}
