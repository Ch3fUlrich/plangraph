//! The merge-close job against a fake keeper: no network, no keeper mutation.
use plangraph_cli::feeder::{Env, Transport};
use plangraph_cli::merge_close::{repos_with, require_seats, run_with};
use serde_json::{json, Value};
use std::cell::RefCell;

struct Fake(Value, RefCell<Vec<(String, Value)>>);

impl Transport for Fake {
    fn call(&self, verb: &str, body: Option<Value>) -> Result<Value, String> {
        self.1.borrow_mut().push((verb.into(), body.unwrap()));
        Ok(self.0.clone())
    }
}

#[test]
fn prints_closed_and_every_listed_key_with_its_reason_and_the_count() {
    let keeper = Fake(
        json!({"closed":[{"key":"demo:A","prs":[{"head":"a1"}]}],
               "would_close":[],
               "listed":[{"key":"demo:S","state":"in_review","reason":"forbidden: sensitive\tcard"}],
               "merged_not_done":1}),
        RefCell::default(),
    );
    let out = run_with(&keeper, Some(true), true, Some("demo")).unwrap();
    assert_eq!(
        out.text,
        "closed\tdemo:A\ta1\nlisted\tdemo:S\tin_review\tforbidden: sensitive card\nmerged-not-done\t1\n"
    );
    assert_eq!(out.merged_not_done, 1);
    let calls = keeper.1.borrow();
    assert_eq!(calls[0].0, "close_merged_auto");
    assert_eq!(
        calls[0].1,
        json!({"require_seats":true,"apply":true,"project":"demo"})
    );
}

#[test]
fn count_mode_asks_for_a_dry_run() {
    let keeper = Fake(
        json!({"closed":[],"would_close":[{"key":"demo:B","prs":[]}],"listed":[],"merged_not_done":1}),
        RefCell::default(),
    );
    let out = run_with(&keeper, Some(false), false, None).unwrap();
    assert!(out.text.starts_with("would-close\tdemo:B\t\n"));
    assert_eq!(
        keeper.1.borrow()[0].1,
        json!({"require_seats":false,"apply":false})
    );
}

#[test]
fn a_reply_without_the_count_is_a_failure_not_a_zero() {
    let keeper = Fake(json!({"closed":[]}), RefCell::default());
    assert!(run_with(&keeper, None, true, None).is_err());
}

#[test]
fn require_seats_is_unset_unless_the_environment_names_it() {
    let mut env = Env::new();
    assert_eq!(require_seats(&env), Ok(None));
    for (value, want) in [
        ("1", Some(true)),
        ("on", Some(true)),
        ("0", Some(false)),
        ("OFF", Some(false)),
        ("false", Some(false)),
        ("", None),
    ] {
        env.insert("PG_MERGE_CLOSE_REQUIRE_SEATS".into(), value.into());
        assert_eq!(require_seats(&env), Ok(want), "{value}");
    }
    env.insert("PG_MERGE_CLOSE_REQUIRE_SEATS".into(), "maybe".into());
    assert!(require_seats(&env).is_err());
}

#[test]
fn the_scanned_repositories_come_from_the_keeper_config() {
    let keeper = Fake(json!({"repos":["data","plangraph"]}), RefCell::default());
    assert_eq!(repos_with(&keeper).unwrap(), ["data", "plangraph"]);
    assert_eq!(keeper.1.borrow()[0].1, json!({"repos_only":true}));
    assert!(repos_with(&Fake(json!({}), RefCell::default())).is_err());
}

#[test]
fn an_unset_switch_sends_no_require_seats_so_the_keeper_setting_decides() {
    let keeper = Fake(
        json!({"closed":[],"would_close":[],"listed":[],"merged_not_done":0}),
        RefCell::default(),
    );
    run_with(&keeper, None, true, None).unwrap();
    assert_eq!(keeper.1.borrow()[0].1, json!({"apply":true}));
}

#[test]
fn a_flagged_close_prints_the_flag_with_its_run_ids() {
    let keeper = Fake(
        json!({"closed":[{"key":"demo:A","prs":[{"head":"a1"}],
                          "flags":{"seats_missing":{"run_ids":["run-1","run-2"],"reason":"x"}}},
                         {"key":"demo:B","prs":[{"head":"b1"}]}],
               "would_close":[{"key":"demo:C","prs":[{"head":"c1"}],
                               "flags":{"seats_missing":{"run_ids":[],"reason":"x"}}}],
               "listed":[],"merged_not_done":3}),
        RefCell::default(),
    );
    let out = run_with(&keeper, None, true, None).unwrap();
    assert_eq!(
        out.text,
        "closed\tdemo:A\ta1\tFLAGGED seats_missing: run-1,run-2\nclosed\tdemo:B\tb1\n\
         would-close\tdemo:C\tc1\tFLAGGED seats_missing\nmerged-not-done\t3\n"
    );
}

#[test]
fn follows_a_cursor_and_reports_scan_errors_once() {
    struct Pages(RefCell<Vec<Value>>);
    impl Transport for Pages {
        fn call(&self, _verb: &str, body: Option<Value>) -> Result<Value, String> {
            let mut calls = self.0.borrow_mut();
            calls.push(body.unwrap());
            let first = calls.len() == 1;
            Ok(json!({
                "would_close": if first { json!([{"key":"demo:A"}]) } else { json!([{"key":"demo:B"}]) },
                "next_after": if first { Some("demo:A") } else { None },
                "scan_errors": {"other": "origin fetch failed"},
                "merged_not_done": 1
            }))
        }
    }
    let keeper = Pages(RefCell::default());
    let out = run_with(&keeper, None, false, Some("demo")).unwrap();
    assert_eq!(out.merged_not_done, 2);
    assert_eq!(keeper.0.borrow()[1]["after"], "demo:A");
    assert_eq!(
        out.text
            .matches("scan-error\tother\torigin fetch failed")
            .count(),
        1
    );
    assert!(out
        .text
        .contains("would-close\tdemo:A\t\nwould-close\tdemo:B\t\n"));
}
