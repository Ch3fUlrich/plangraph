use super::settle::{HostReceipt, PrState, Settlement};
use super::settle_cli::{parse_hosts, parse_pr_view, run};
use std::collections::BTreeMap;

fn flags(pairs: &[(&str, &str)]) -> BTreeMap<String, String> {
    pairs
        .iter()
        .map(|(k, v)| ((*k).into(), (*v).into()))
        .collect()
}

#[test]
fn gh_pull_request_state_is_parsed() {
    let head = "a".repeat(40);
    for (word, state) in [
        ("OPEN", PrState::Open),
        ("CLOSED", PrState::Closed),
        ("MERGED", PrState::Merged),
    ] {
        let raw = format!(r#"{{"state":"{word}","headRefOid":"{head}"}}"#);
        assert_eq!(parse_pr_view(&raw), Ok((state, head.clone())));
    }
    assert!(parse_pr_view(r#"{"state":"DRAFT","headRefOid":"x"}"#).is_err());
    assert!(parse_pr_view("not json").is_err());
}

#[test]
fn host_receipts_are_parsed_from_verify_hosts() {
    let head = "b".repeat(40);
    let raw = format!(
        r#"{{"verified":[{{"host":"one","head":"{head}","synced":false}},{{"host":"two","head":"{head}"}}],"count":2}}"#
    );
    assert_eq!(
        parse_hosts(&raw).unwrap(),
        vec![
            HostReceipt {
                host: "one".into(),
                head: head.clone()
            },
            HostReceipt {
                host: "two".into(),
                head
            },
        ]
    );
    assert!(parse_hosts(r#"{"verified":[{"host":"one"}]}"#).is_err());
}

fn init_file(dir: &std::path::Path, merge: &str, parts: &str) -> String {
    let path = dir.join("init.json");
    std::fs::write(
        &path,
        format!(r#"{{"repo":"example/repo","train_pr":9,"merge":"{merge}","parts":{parts}}}"#),
    )
    .unwrap();
    path.to_string_lossy().into_owned()
}

#[test]
fn init_refuses_a_short_sha_and_a_duplicate_part() {
    let dir = tempfile::tempdir().unwrap();
    let state = dir.path().join("s.json");
    let state = state.to_string_lossy().into_owned();
    let long = "c".repeat(40);
    let part = |pr: u64| format!(r#"{{"pr":{pr},"head":"{long}","items":["k:A"],"folded":true}}"#);
    let short = init_file(dir.path(), "abc", "[]");
    let err = run(&flags(&[
        ("state", &state),
        ("init", &short),
        ("repo-dir", "."),
    ]))
    .unwrap_err();
    assert!(err.contains("full lower-case SHA"), "{err}");
    let dup = init_file(dir.path(), &long, &format!("[{},{}]", part(1), part(1)));
    let err = run(&flags(&[
        ("state", &state),
        ("init", &dup),
        ("repo-dir", "."),
    ]))
    .unwrap_err();
    assert!(err.contains("twice"), "{err}");
    assert!(Settlement::load(std::path::Path::new(&state))
        .unwrap()
        .is_none());
}

#[test]
fn a_settlement_with_no_parts_needs_no_effect_but_the_hosts() {
    // The state file is the entry point: a saved settlement resumes without --init.
    let dir = tempfile::tempdir().unwrap();
    let state = dir.path().join("s.json");
    let mut done = Settlement::new("example/repo", 9, &"d".repeat(40), vec![]);
    done.hosts = super::settle::Step::Done("3 hosts".into());
    done.hold_released = true;
    done.save(&state).unwrap();
    let lines = run(&flags(&[
        ("state", &state.to_string_lossy()),
        ("repo-dir", "."),
    ]))
    .unwrap();
    assert_eq!(lines, vec!["settle #9: 0 steps done, 0 held, complete"]);
}
