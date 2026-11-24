use super::deploy_post::*;
use super::deploy_post_tests::{active, gate, oneshot_ok, run, target, Host, Rows};

fn ran_at(timestamp: &str) -> Host {
    let mut host = Host::default();
    let mut run = oneshot_ok();
    run.exec_main_exit_timestamp = Some(timestamp.into());
    host.runs.insert("a.service".into(), run);
    host.runs.insert("a.timer".into(), active());
    host
}

#[test]
fn an_unparsable_stale_or_future_exit_time_is_a_failed_first_run_through_the_disable_path() {
    for bad in [
        "t",
        "",
        "2001-01-01T00:00:00Z",        // before the landed head (stale run)
        "2099-01-01T00:00:00Z",        // after "now"
        "Mon 2001-09-09 01:46:39 UTC", // stale, systemd form
    ] {
        let host = ran_at(bad);
        let rows = Rows::default();
        let mut state = PostState::default();
        let r = run(
            &[target("a.service", "alpha", &["demo:A"])],
            &host,
            &rows,
            &mut state,
        )
        .unwrap();
        assert!(r.failed.is_some(), "{bad:?} must fail");
        assert!(rows.0.borrow().is_empty(), "{bad:?} wrote a row");
        assert!(host
            .calls
            .borrow()
            .contains(&"disable a.timer@alpha".to_string()));
    }
    let host = ran_at("Tue 2026-11-24 01:00:00 UTC");
    let rows = Rows::default();
    let mut state = PostState::default();
    let r = run(
        &[target("a.service", "alpha", &["demo:A"])],
        &host,
        &rows,
        &mut state,
    )
    .unwrap();
    assert_eq!(r.deployed, ["a.service@alpha"]);
}

#[test]
fn exit_epoch_reads_both_systemd_forms() {
    assert_eq!(
        exit_epoch("2026-11-24T01:00:00Z"),
        exit_epoch("Tue 2026-11-24 01:00:00 UTC")
    );
    assert_eq!(exit_epoch("1970-01-01T00:00:10Z"), Some(10));
    assert_eq!(exit_epoch("t"), None);
}

#[test]
fn a_service_gets_no_row_when_its_shipped_timer_was_refused_at_preflight() {
    let mut host = Host::default();
    host.runs.insert("a.service".into(), oneshot_ok());
    host.runs.insert("a.timer".into(), active()); // an older, still active timer on the host
    let rows = Rows::default();
    let mut state = PostState::default();
    let mut g = gate();
    g.excluded.insert(("a.timer".into(), "alpha".into()));
    let r = apply(
        &[target("a.service", "alpha", &["demo:A"])],
        &g,
        &host,
        &rows,
        &mut state,
        &mut |_| Ok(()),
    )
    .unwrap();
    assert!(r.failed.unwrap().1.contains("refused at preflight"));
    assert!(rows.0.borrow().is_empty());
}
