//! The deploy part of the GO: `land-train go-verify` refuses a green train while a manifest entry is over
//! 24 h not live, through the entry point the router calls.
use super::cli::run_inner;
use super::go_verify::deploy_refusals;
use super::go_verify_tests::green;
use crate::deploy_manifest::ledger::{self, Ledger};
use std::path::Path;

fn now() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs() as i64
}

const REGISTRY: &str = r#"{"version":1,"self":"host-a","hosts":[
 {"id":"host-a","aliases":[],"transport":"local"}]}"#;

fn write_ledger(dir: &Path, hours_not_live: Option<i64>) {
    let mut l = Ledger {
        written_at: now(),
        ..Ledger::default()
    };
    if let Some(hours) = hours_not_live {
        l.not_live_since
            .insert("unit:pg-feeder".into(), now() - hours * 3600);
        l.state
            .insert("unit:pg-feeder".into(), "not-installed".into());
    }
    ledger::save(&dir.join("host-a.json"), &l).unwrap();
}

fn go_verify(dir: &Path) -> Result<Vec<String>, String> {
    let evidence = dir.join("evidence.json");
    std::fs::write(&evidence, serde_json::to_vec(&green()).unwrap()).unwrap();
    std::fs::write(dir.join("hosts.json"), REGISTRY).unwrap();
    let args: Vec<String> = [
        "go-verify",
        "--evidence",
        evidence.to_str().unwrap(),
        "--deploy-ledgers",
        dir.join("ledgers").to_str().unwrap(),
        "--hosts",
        dir.join("hosts.json").to_str().unwrap(),
    ]
    .map(String::from)
    .to_vec();
    run_inner(dir, &args, &|| crate::base_fresh::python_command(None))
}

#[test]
fn a_green_train_is_verified_while_every_entry_is_live_or_young() {
    let dir = tempfile::tempdir().unwrap();
    write_ledger(&dir.path().join("ledgers"), Some(3));
    let lines = go_verify(dir.path()).unwrap();
    assert!(lines[0].starts_with("GO-VERIFIED"), "{lines:?}");
}

#[test]
fn a_green_train_is_refused_for_an_entry_over_24_hours_not_live() {
    let dir = tempfile::tempdir().unwrap();
    write_ledger(&dir.path().join("ledgers"), Some(30));
    let refused = go_verify(dir.path()).unwrap_err();
    assert!(
        refused.contains("deploy_not_live")
            && refused.contains("unit:pg-feeder")
            && refused.contains("30h"),
        "{refused}"
    );
}

#[test]
fn a_train_is_refused_when_no_host_has_reported() {
    let dir = tempfile::tempdir().unwrap();
    assert!(go_verify(dir.path())
        .unwrap_err()
        .contains("deploy_ledger_missing"));
}

#[test]
fn the_compiled_in_exceptions_parse_and_refusals_carry_the_stable_codes() {
    let dir = tempfile::tempdir().unwrap();
    write_ledger(dir.path(), Some(48));
    let compiled = include_str!("../../../../deploy/exceptions.toml");
    let refusals = deploy_refusals(dir.path(), compiled, Some(REGISTRY), now()).unwrap();
    assert_eq!(
        refusals.iter().map(|r| r.code).collect::<Vec<_>>(),
        ["deploy_not_live"]
    );
    let excused =
        "[[exception]]\nentry='unit:pg-feeder'\nuntil='2999-01-01'\ndecision='D-1'\nreason='r'\n";
    assert!(deploy_refusals(dir.path(), excused, Some(REGISTRY), now())
        .unwrap()
        .is_empty());
    assert!(deploy_refusals(
        dir.path(),
        "[[exception]]\nentry='x'\n",
        Some(REGISTRY),
        now()
    )
    .is_err());
}

#[test]
fn a_train_is_refused_while_a_registry_host_never_reported_even_with_one_fresh_ledger() {
    let dir = tempfile::tempdir().unwrap();
    write_ledger(&dir.path().join("ledgers"), None);
    let two = r#"{"version":1,"self":"host-a","hosts":[
      {"id":"host-a","aliases":[],"transport":"local"},
      {"id":"host-b","aliases":[],"transport":"ssh","ssh_target":"b"}]}"#;
    let refusals = deploy_refusals(&dir.path().join("ledgers"), "", Some(two), now()).unwrap();
    assert_eq!(refusals.len(), 1, "{refusals:?}");
    assert!(
        refusals[0].code == "deploy_ledger_missing" && refusals[0].detail.starts_with("host-b")
    );
    let unreadable = deploy_refusals(&dir.path().join("ledgers"), "", None, now()).unwrap();
    assert_eq!(unreadable[0].code, "deploy_hosts_unknown");
}
