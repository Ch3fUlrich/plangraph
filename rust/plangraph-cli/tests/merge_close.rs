//! The merge-close job against a fake keeper: no network, no keeper mutation.
use plangraph_cli::feeder::{Env, Transport};
use plangraph_cli::merge_close::{
    repos_with, require_seats, run_scheduled_with, run_with, ScheduleTiming,
};
use serde_json::{json, Value};
use std::cell::RefCell;
use std::collections::VecDeque;
use std::time::Duration;

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

#[test]
fn reports_unproven_deployments_with_their_merge_age() {
    let keeper = Fake(
        json!({
            "merged_not_done": 2,
            "deployment_pending": [
                {"key": "demo:A", "age_hours": 24, "missing": ["fixture:unit.timer"]},
                {"key": "demo:B", "age_hours": 23, "missing": ["live_evidence_missing"]}
            ]
        }),
        RefCell::default(),
    );
    let outcome = run_with(&keeper, None, false, Some("demo")).unwrap();
    assert!(outcome
        .text
        .contains("deployment-pending\tdemo:A\t24h\tfixture:unit.timer"));
    assert!(outcome
        .text
        .contains("deployment-pending\tdemo:B\t23h\tlive_evidence_missing"));
    assert!(outcome.text.contains("merged-not-live\t2\n"));
}

struct Script(RefCell<VecDeque<(Value, Value)>>);

impl Script {
    fn new<const N: usize>(steps: [(Value, Value); N]) -> Self {
        Self(RefCell::new(steps.into()))
    }

    fn assert_finished(&self) {
        assert!(self.0.borrow().is_empty(), "unconsumed keeper calls");
    }
}

impl Transport for Script {
    fn call(&self, verb: &str, body: Option<Value>) -> Result<Value, String> {
        let (expected, reply) = self
            .0
            .borrow_mut()
            .pop_front()
            .expect("unexpected keeper call");
        assert_eq!(verb, "close_merged_auto");
        assert_eq!(body, Some(expected));
        match reply.as_str() {
            Some(error) => Err(error.into()),
            None => Ok(reply),
        }
    }
}

#[test]
fn scheduled_close_refreshes_then_reads_each_project_page_in_order() {
    let keeper = Script::new([
        (json!({"refresh":"start"}), json!({"scan":"running"})),
        (json!({"refresh":"status"}), json!({"scan":"running"})),
        (json!({"refresh":"status"}), json!({"scan":"ready"})),
        (
            json!({"refresh":"projects"}),
            json!({"projects":["demo","other"]}),
        ),
        (
            json!({"apply":true,"project":"demo","require_seats":false}),
            json!({"closed":[{"key":"demo:A"}],"merged_not_done":0,"next_after":"demo:A"}),
        ),
        (
            json!({"apply":true,"project":"demo","require_seats":false,"after":"demo:A"}),
            json!({"closed":[{"key":"demo:B"}],"merged_not_done":0}),
        ),
        (
            json!({"apply":true,"project":"other","require_seats":false}),
            json!({"listed":[{"key":"other:A","state":"open","reason":"open lane"}],"merged_not_done":1}),
        ),
    ]);
    let outcome = run_scheduled_with(
        &keeper,
        &keeper,
        Some(false),
        true,
        None,
        false,
        ScheduleTiming::new(Duration::from_secs(95), Duration::from_secs(400)),
    )
    .unwrap();
    assert_eq!(outcome.merged_not_done, 1);
    assert!(outcome
        .text
        .starts_with("closed\tdemo:A\t\nclosed\tdemo:B\t\nlisted\tother:A\t"));
    keeper.assert_finished();
}

#[test]
fn scheduled_close_stops_at_the_refresh_deadline_without_paging() {
    let control = Script::new([
        (json!({"refresh":"start"}), json!({"scan":"running"})),
        (json!({"refresh":"status"}), json!({"scan":"running"})),
    ]);
    let keeper = Script::new([]);
    let error = run_scheduled_with(
        &keeper,
        &control,
        None,
        true,
        None,
        false,
        ScheduleTiming::new(Duration::ZERO, Duration::from_secs(400)),
    )
    .unwrap_err();
    assert_eq!(error, "merge index refresh is still running");
    control.assert_finished();
    keeper.assert_finished();
}

#[test]
fn expiry_refreshes_and_retries_the_same_cursor() {
    let first = json!({"apply":true,"project":"demo","require_seats":false});
    let next = json!({"apply":true,"project":"demo","require_seats":false,"after":"demo:A"});
    let keeper = Script::new([
        (json!({"refresh":"start"}), json!({"scan":"running"})),
        (json!({"refresh":"status"}), json!({"scan":"ready"})),
        (
            first,
            json!({"closed":[{"key":"demo:A"}],"merged_not_done":0,"next_after":"demo:A"}),
        ),
        (
            next.clone(),
            json!("item_refused:conflict:merge index expired; start a refresh"),
        ),
        (json!({"refresh":"start"}), json!({"scan":"running"})),
        (json!({"refresh":"status"}), json!({"scan":"ready"})),
        (
            next,
            json!({"closed":[{"key":"demo:B"}],"merged_not_done":0}),
        ),
    ]);
    let out = run_scheduled_with(
        &keeper,
        &keeper,
        Some(false),
        true,
        Some("demo"),
        false,
        ScheduleTiming::new(Duration::ZERO, Duration::from_secs(400)),
    )
    .unwrap();
    assert!(out.text.starts_with("closed\tdemo:A\t\nclosed\tdemo:B\t\n"));
    assert!(!out.text.contains("continue\t"));
    keeper.assert_finished();
}

#[test]
fn successful_pages_stop_at_the_pass_deadline_after_recovery() {
    const MERGE_INDEX_EXPIRED: &str = "item_refused:conflict:merge index expired; start a refresh";
    struct DelayedRetry(Script);
    impl Transport for DelayedRetry {
        fn call(&self, verb: &str, body: Option<Value>) -> Result<Value, String> {
            let result = self.0.call(verb, body)?;
            if result["next_after"] == "demo:32" {
                std::thread::sleep(Duration::from_secs(1));
            }
            Ok(result)
        }
    }
    let closed = |keys: std::ops::RangeInclusive<u32>| {
        keys.map(|n| json!({"key":format!("demo:{n:02}")}))
            .collect::<Vec<_>>()
    };
    let start = (json!({"refresh":"start"}), json!({"scan":"running"}));
    let ready = (json!({"refresh":"status"}), json!({"scan":"ready"}));
    let page = json!({"apply":true,"project":"demo","after":"demo:25"});
    let keeper = DelayedRetry(Script::new([
        start.clone(),
        ready.clone(),
        (
            json!({"apply":true,"project":"demo"}),
            json!({"closed":closed(1..=25),"merged_not_done":0,"next_after":"demo:25"}),
        ),
        (page.clone(), json!(MERGE_INDEX_EXPIRED)),
        start,
        ready,
        (
            page,
            json!({"closed":closed(26..=32),"merged_not_done":0,"next_after":"demo:32"}),
        ),
    ]));
    let timing = ScheduleTiming::new(Duration::ZERO, Duration::from_secs(1));
    let out =
        run_scheduled_with(&keeper, &keeper, None, true, Some("demo"), false, timing).unwrap();
    assert_eq!(out.text.matches("closed\tdemo:").count(), 32);
    assert!(out.text.contains(&format!(
        "continue\t1 projects remaining\t{MERGE_INDEX_EXPIRED}\n"
    )));
    keeper.0.assert_finished();
}

#[test]
fn expiry_after_a_close_exits_successfully_if_refresh_cannot_finish() {
    let keeper = Script::new([
        (json!({"refresh":"start"}), json!({"scan":"running"})),
        (json!({"refresh":"status"}), json!({"scan":"ready"})),
        (
            json!({"refresh":"projects"}),
            json!({"projects":["demo","other"]}),
        ),
        (
            json!({"apply":true,"project":"demo"}),
            json!({"closed":[{"key":"demo:A"}],"merged_not_done":0,"next_after":"demo:A"}),
        ),
        (
            json!({"apply":true,"project":"demo","after":"demo:A"}),
            json!("item_refused:conflict:merge index expired; start a refresh"),
        ),
        (json!({"refresh":"start"}), json!({"scan":"running"})),
        (json!({"refresh":"status"}), json!({"scan":"running"})),
    ]);
    let out = run_scheduled_with(
        &keeper,
        &keeper,
        None,
        true,
        None,
        false,
        ScheduleTiming::new(Duration::ZERO, Duration::from_secs(400)),
    )
    .unwrap();
    assert!(out.text.contains("closed\tdemo:A\t\n"));
    assert!(out.text.contains("continue\t2 projects remaining\t"));
    keeper.assert_finished();
}

#[test]
fn running_refresh_conflict_without_a_close_is_an_error() {
    let keeper = Script::new([
        (json!({"refresh":"start"}), json!({"scan":"running"})),
        (json!({"refresh":"status"}), json!({"scan":"ready"})),
        (
            json!({"apply":true,"project":"demo"}),
            json!("item_refused:conflict:merge index refresh is running"),
        ),
    ]);
    assert_eq!(
        run_scheduled_with(
            &keeper,
            &keeper,
            None,
            true,
            Some("demo"),
            false,
            ScheduleTiming::new(Duration::ZERO, Duration::from_secs(400))
        )
        .unwrap_err(),
        "item_refused:conflict:merge index refresh is running",
    );
    keeper.assert_finished();
}

#[test]
fn scheduled_preview_fails_if_the_selected_project_scan_is_incomplete() {
    let keeper = Script::new([
        (json!({"refresh":"start"}), json!({"scan":"running"})),
        (json!({"refresh":"status"}), json!({"scan":"ready"})),
        (
            json!({"apply":false,"project":"demo"}),
            json!({"scan_errors":{"demo":"origin fetch failed"},"merged_not_done":0}),
        ),
    ]);
    let error = run_scheduled_with(
        &keeper,
        &keeper,
        None,
        false,
        Some("demo"),
        false,
        ScheduleTiming::new(Duration::ZERO, Duration::from_secs(400)),
    )
    .unwrap_err();
    assert_eq!(
        error,
        "incomplete merge scan: {\"demo\": \"origin fetch failed\"}"
    );
    keeper.assert_finished();
}
