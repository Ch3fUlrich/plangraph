use super::guard::{check, parse_exceptions, read_ledgers, Exception, LEDGER_MAX_AGE_HOURS};
use super::ledger::Ledger;

const NOW: i64 = 1_800_000_000; // 2027-01-15

fn ledger(key: &str, hours_ago: i64) -> (String, Ledger) {
    let mut l = Ledger {
        written_at: NOW - 60,
        ..Ledger::default()
    };
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
    assert_eq!(check(&[], &[], NOW)[0].code, "deploy_ledger_missing");
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
