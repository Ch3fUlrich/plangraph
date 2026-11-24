use super::guard::{
    check as check_pinned, expected_hosts, parse_exceptions, read_ledgers, Exception, ExpectedHost,
    Finding, Pins, LEDGER_MAX_AGE_HOURS,
};
use super::ledger::Ledger;

const NOW: i64 = 1_800_000_000; // 2027-01-15

fn fresh() -> Ledger {
    Ledger {
        written_at: NOW - 60,
        manifest: Some(pins().manifest),
        stamp: Some(pins().stamp),
        ..Ledger::default()
    }
}

fn host_a() -> Vec<ExpectedHost> {
    vec![ExpectedHost {
        id: "host-a".into(),
        aliases: vec![],
    }]
}

fn pins() -> Pins {
    Pins {
        manifest: "m".repeat(64),
        stamp: "s".repeat(40),
    }
}

fn check_hosts(
    ledgers: &[(String, Ledger)],
    expected: &[ExpectedHost],
    exceptions: &[Exception],
    now: i64,
) -> Vec<Finding> {
    check_pinned(ledgers, expected, &pins(), exceptions, now)
}

fn check(ledgers: &[(String, Ledger)], exceptions: &[Exception], now: i64) -> Vec<Finding> {
    check_hosts(ledgers, &host_a(), exceptions, now)
}

fn ledger(key: &str, hours_ago: i64) -> (String, Ledger) {
    let mut l = fresh();
    l.not_live_since.insert(key.into(), NOW - hours_ago * 3600);
    l.state.insert(key.into(), "not-installed".into());
    ("host-a".into(), l)
}

fn exception(entry: &str, until: &str) -> Vec<Exception> {
    parse_exceptions(&format!(
        "[[exception]]\nentry = \"{entry}\"\nuntil = \"{until}\"\ndecision = \"D-4000\"\nreason = \"waiting for the key placement\"\n"
    ))
    .unwrap()
}

#[test]
fn an_entry_not_live_for_more_than_24_hours_refuses_and_names_host_entry_and_age() {
    let findings = check(&[ledger("unit:pg-feeder", 25)], &[], NOW);
    assert_eq!(findings.len(), 1, "{findings:?}");
    assert_eq!(findings[0].code, "deploy_not_live");
    for part in ["host-a", "unit:pg-feeder", "not-installed", "25h"] {
        assert!(findings[0].detail.contains(part), "{}", findings[0].detail);
    }
}

#[test]
fn an_entry_not_live_for_24_hours_or_less_does_not_refuse() {
    assert!(check(&[ledger("unit:pg-feeder", 24)], &[], NOW).is_empty());
    assert!(check(&[ledger("unit:pg-feeder", 1)], &[], NOW).is_empty());
}

#[test]
fn a_dated_routing_exception_excuses_only_its_entry_until_the_end_of_its_day() {
    let ledgers = [ledger("unit:pg-feeder", 100)];
    assert!(
        check(&ledgers, &exception("unit:pg-feeder", "2027-01-15"), NOW).is_empty(),
        "the day itself is covered"
    );
    assert_eq!(
        check(&ledgers, &exception("unit:pg-feeder", "2027-01-14"), NOW).len(),
        1,
        "an expired exception excuses nothing"
    );
    assert_eq!(
        check(&ledgers, &exception("unit:pg-other", "2027-02-01"), NOW).len(),
        1,
        "another entry's exception does not"
    );
}

#[test]
fn an_empty_or_stale_ledger_refuses_instead_of_passing() {
    let missing = check(&[], &[], NOW);
    assert_eq!(missing[0].code, "deploy_ledger_missing");
    assert!(missing[0].detail.contains("host-a"));
    assert_eq!(
        check_hosts(&[], &[], &[], NOW)[0].code,
        "deploy_hosts_unknown",
        "no expected host is a refusal, not a pass"
    );
    let (host, mut l) = ledger("unit:x", 1);
    l.written_at = NOW - (LEDGER_MAX_AGE_HOURS + 1) * 3600;
    let findings = check(&[(host, l)], &[], NOW);
    assert_eq!(findings[0].code, "deploy_ledger_stale");
}

#[test]
fn exceptions_need_a_d_id_a_date_and_a_reason() {
    let bad = |decision: &str, until: &str, reason: &str| {
        parse_exceptions(&format!("[[exception]]\nentry='unit:x'\nuntil='{until}'\ndecision='{decision}'\nreason='{reason}'\n"))
    };
    assert!(bad("D-1", "2027-01-01", "r").is_ok());
    assert!(bad("1", "2027-01-01", "r").unwrap_err().contains("D-id"));
    assert!(bad("D-1", "soon", "r").unwrap_err().contains("date"));
    assert!(bad("D-1", "2027-01-01", " ")
        .unwrap_err()
        .contains("reason"));
    assert!(parse_exceptions("").unwrap().is_empty());
}

#[test]
fn ledgers_are_read_from_a_directory_and_a_broken_file_is_a_finding() {
    let dir = tempfile::tempdir().unwrap();
    let (_, good) = ledger("unit:x", 30);
    std::fs::write(
        dir.path().join("host-a.json"),
        serde_json::to_vec(&good).unwrap(),
    )
    .unwrap();
    std::fs::write(dir.path().join("host-b.json"), "not json").unwrap();
    std::fs::write(dir.path().join("note.txt"), "ignored").unwrap();
    let (ledgers, findings) = read_ledgers(dir.path());
    assert_eq!(ledgers.len(), 1);
    assert_eq!(ledgers[0].0, "host-a");
    assert_eq!(findings[0].code, "deploy_ledger_unreadable");
}

const REGISTRY: &str = r#"{"version":1,"self":"center","hosts":[
 {"id":"center","aliases":["coding-vm"],"transport":"local"},
 {"id":"dell","aliases":[],"transport":"ssh","ssh_target":"dell-ssh"},
 {"id":"odd","aliases":[],"transport":"keeper"}]}"#;

#[test]
fn one_fresh_ledger_is_not_enough_while_another_expected_host_never_reported() {
    let expected = expected_hosts(REGISTRY).unwrap();
    assert_eq!(
        expected.len(),
        2,
        "the keeper-transport host has no ledger to ask for"
    );
    let only_center = [("center".to_owned(), fresh())];
    let findings = check_hosts(&only_center, &expected, &[], NOW);
    assert_eq!(findings.len(), 1, "{findings:?}");
    assert_eq!(findings[0].code, "deploy_ledger_missing");
    assert!(
        findings[0].detail.starts_with("dell:"),
        "{}",
        findings[0].detail
    );
}

#[test]
fn a_ledger_under_an_alias_counts_and_the_newest_file_of_a_host_decides() {
    let expected = expected_hosts(REGISTRY).unwrap();
    let mut old = fresh();
    old.written_at = NOW - 100 * 3600;
    let ledgers = [
        ("center".to_owned(), old),
        ("Coding-VM".to_owned(), fresh()),
        ("dell".to_owned(), fresh()),
    ];
    assert!(check_hosts(&ledgers, &expected, &[], NOW).is_empty());
}

#[test]
fn a_ledger_of_a_host_outside_the_registry_is_not_asked_about() {
    let expected = expected_hosts(REGISTRY).unwrap();
    let mut stale = fresh();
    stale.written_at = 1;
    let ledgers = [
        ("center".to_owned(), fresh()),
        ("dell".to_owned(), fresh()),
        ("retired".to_owned(), stale),
    ];
    assert!(check_hosts(&ledgers, &expected, &[], NOW).is_empty());
}

#[test]
fn the_limit_is_compared_in_seconds_so_24_hours_and_one_second_refuses() {
    let (host, mut l) = ledger("unit:pg-feeder", 24);
    l.not_live_since
        .insert("unit:pg-feeder".into(), NOW - 24 * 3600 - 1);
    let findings = check(&[(host.clone(), l.clone())], &[], NOW);
    assert_eq!(findings.len(), 1, "{findings:?}");
    assert_eq!(findings[0].code, "deploy_not_live");
    l.not_live_since
        .insert("unit:pg-feeder".into(), NOW - 24 * 3600);
    assert!(
        check(&[(host, l)], &[], NOW).is_empty(),
        "exactly 24 h is inside the limit"
    );
}

#[test]
fn a_ledger_six_hours_and_one_second_old_is_stale() {
    let (host, mut l) = ledger("unit:x", 1);
    l.written_at = NOW - LEDGER_MAX_AGE_HOURS * 3600 - 1;
    assert_eq!(check(&[(host, l)], &[], NOW)[0].code, "deploy_ledger_stale");
}

#[test]
fn a_report_for_older_inputs_is_refused_even_when_it_is_fresh_and_clean() {
    let mut old_manifest = fresh();
    old_manifest.manifest = Some("a".repeat(64));
    let findings = check(&[("host-a".into(), old_manifest)], &[], NOW);
    assert_eq!(findings.len(), 1, "{findings:?}");
    assert_eq!(findings[0].code, "deploy_ledger_outdated");
    assert!(
        findings[0].detail.starts_with("host-a:"),
        "{}",
        findings[0].detail
    );
    let mut old_release = fresh();
    old_release.stamp = Some("b".repeat(40));
    assert_eq!(
        check(&[("host-a".into(), old_release)], &[], NOW)[0].code,
        "deploy_ledger_outdated"
    );
    let mut unpinned = fresh();
    unpinned.manifest = None;
    unpinned.stamp = None;
    assert_eq!(
        check(&[("host-a".into(), unpinned)], &[], NOW)[0].code,
        "deploy_ledger_outdated",
        "a ledger without the pins is from before they existed"
    );
    assert!(check(&[("host-a".into(), fresh())], &[], NOW).is_empty());
}
