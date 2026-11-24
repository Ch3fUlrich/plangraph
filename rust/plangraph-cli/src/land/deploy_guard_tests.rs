//! `land-train deploy-guard`: the landing step's own deploy check, with its refresh through a fake runner.
use super::deploy_guard::run_with;
use super::go_deploy_tests::{pinned_ledger, pinned_repo};
use crate::deploy_manifest::fleet::{FleetHost, Remote};
use crate::deploy_manifest::guard::Pins;
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

/// The fleet as the refresh sees it: each host answers with the ledger the test gives it, or fails.
struct Fleet {
    answers: BTreeMap<String, Result<Option<Ledger>, String>>,
    asked: Mutex<Vec<String>>,
}

impl Remote for Fleet {
    fn run(&self, host: &FleetHost, _: &[String]) -> Result<Out, String> {
        self.asked.lock().unwrap().push(host.id.clone());
        match self.answers.get(&host.id) {
            Some(Ok(Some(ledger))) => Ok(Out {
                code: 0,
                stdout: format!(
                    "pending\t0\nledger\t{}\n",
                    serde_json::to_string(ledger).unwrap()
                ),
                stderr: String::new(),
            }),
            // Answered, but handed no ledger back (an older host-sync binary).
            Some(Ok(None)) => Ok(Out {
                code: 0,
                stdout: "pending\t0\n".into(),
                stderr: String::new(),
            }),
            Some(Err(why)) => Err(why.clone()),
            None => Err("no answer".into()),
        }
    }
}

struct World {
    dir: tempfile::TempDir,
    pins: Pins,
}

fn world() -> World {
    let dir = tempfile::tempdir().unwrap();
    let (_, pins) = pinned_repo(dir.path());
    World { dir, pins }
}

impl World {
    fn flags(&self, refresh: bool) -> BTreeMap<String, String> {
        let dir = self.dir.path();
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
            (
                "repo".to_owned(),
                dir.join("checkout").display().to_string(),
            ),
        ]);
        if refresh {
            flags.insert("refresh".into(), "yes".into());
        }
        flags
    }

    fn ledger(&self, hours_not_live: Option<i64>) -> Ledger {
        let mut l = pinned_ledger(&self.pins);
        if let Some(h) = hours_not_live {
            l.not_live_since
                .insert("unit:pg-feeder".into(), now() - h * 3600 - 60);
            l.state
                .insert("unit:pg-feeder".into(), "not-installed".into());
        }
        l
    }

    /// A ledger file an earlier run left behind.
    fn cached(&self, host: &str, ledger: &Ledger) {
        ledger::save(
            &self.dir.path().join("ledgers").join(format!("{host}.json")),
            ledger,
        )
        .unwrap();
    }
}

fn fleet(answers: Vec<(&str, Result<Option<Ledger>, String>)>) -> Fleet {
    Fleet {
        answers: answers
            .into_iter()
            .map(|(h, a)| (h.to_owned(), a))
            .collect(),
        asked: Mutex::new(Vec::new()),
    }
}

#[test]
fn the_refresh_asks_every_registry_host_and_a_complete_fleet_passes() {
    let w = world();
    let f = fleet(vec![
        ("center", Ok(Some(w.ledger(None)))),
        ("dell", Ok(Some(w.ledger(Some(2))))),
    ]);
    let lines = run_with(&w.flags(true), &f).unwrap();
    let mut ids = f.asked.lock().unwrap().clone();
    ids.sort();
    assert_eq!(ids, ["center", "dell"]);
    assert!(
        lines.last().unwrap().starts_with("DEPLOY-LIVE"),
        "{lines:?}"
    );
}

#[test]
fn a_failed_refresh_does_not_pass_on_cached_ledgers_under_six_hours_old() {
    let w = world();
    // Both hosts have a fresh-looking ledger from an earlier run; neither answers now.
    w.cached("center", &w.ledger(None));
    w.cached("dell", &w.ledger(None));
    let f = fleet(vec![
        ("center", Err("ssh: connection refused".into())),
        ("dell", Ok(None)),
    ]);
    let refused = run_with(&w.flags(true), &f).unwrap_err();
    assert!(
        refused.contains("deploy_refresh_failed: center: unreachable: ssh: connection refused"),
        "{refused}"
    );
    assert!(
        refused.contains("deploy_refresh_failed: dell: install run answered without a ledger"),
        "{refused}"
    );
    // The same cached ledgers are fine for a check that does not claim to refresh.
    assert!(run_with(&w.flags(false), &f).is_ok());
}

#[test]
fn an_overdue_entry_on_any_host_prevents_landing() {
    let w = world();
    w.cached("center", &w.ledger(None));
    w.cached("dell", &w.ledger(Some(30)));
    let refused = run_with(&w.flags(false), &fleet(vec![])).unwrap_err();
    assert!(
        refused.contains("deploy_not_live") && refused.contains("dell") && refused.contains("30h"),
        "{refused}"
    );
}

#[test]
fn a_host_on_older_inputs_prevents_landing_even_with_a_clean_report() {
    let w = world();
    let mut old = w.ledger(None);
    old.manifest = Some("0".repeat(64));
    let f = fleet(vec![
        ("center", Ok(Some(w.ledger(None)))),
        ("dell", Ok(Some(old))),
    ]);
    let refused = run_with(&w.flags(true), &f).unwrap_err();
    assert!(
        refused.contains("deploy_ledger_outdated: dell"),
        "{refused}"
    );
}

#[test]
fn a_host_that_never_reported_prevents_landing_and_no_registry_prevents_it_too() {
    let w = world();
    w.cached("center", &w.ledger(None));
    let refused = run_with(&w.flags(false), &fleet(vec![])).unwrap_err();
    assert!(
        refused.contains("deploy_ledger_missing") && refused.contains("dell"),
        "{refused}"
    );
    let mut none = w.flags(false);
    none.insert(
        "hosts".into(),
        w.dir.path().join("absent.json").display().to_string(),
    );
    let refused = run_with(&none, &fleet(vec![])).unwrap_err();
    assert!(refused.contains("deploy_hosts_unknown"), "{refused}");
}

#[test]
fn a_refresh_without_a_registry_is_an_error_not_a_pass() {
    let w = world();
    let mut f = w.flags(true);
    f.insert(
        "hosts".into(),
        w.dir.path().join("absent.json").display().to_string(),
    );
    assert!(run_with(&f, &fleet(vec![]))
        .unwrap_err()
        .contains("registry"));
}
