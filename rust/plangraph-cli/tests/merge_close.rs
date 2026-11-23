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
fn reconcile_persists_one_routing_batch_and_only_requests_a_watched_dry_run() {
    use plangraph_cli::merge_close::reconcile_with;

    let keeper = Fake(
        json!({
            "merged_not_done": 3, "alert": true,
            "would_close": [{"key":"demo:A"}],
            "listed": [
                {"key":"demo:B","reason":"evidence_unverified: unmerged_work_or_lanes_unknown"},
                {"key":"demo:C","reason":"evidence_unverified: deploy_pending: service"}
            ]
        }),
        RefCell::default(),
    );
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("watch.json");
    let line = reconcile_with(&keeper, Some(true), &path).unwrap();
    assert_eq!(line.lines().count(), 1);
    let batch: Value =
        serde_json::from_str(line.strip_prefix("reconcile-merged\t").unwrap()).unwrap();
    assert_eq!(
        serde_json::from_slice::<Value>(&std::fs::read(path).unwrap()).unwrap(),
        batch
    );
    assert_eq!(batch["alert"], true);
    assert_eq!(
        batch["refusals"],
        json!({"deploy_pending":1,"unmerged_work_or_lanes_unknown":1})
    );
    assert_eq!(batch["proposals"][0]["action"], "CLOSE");
    assert_eq!(batch["proposals"][1]["action"], "SLICE-OPEN");
    assert_eq!(batch["proposals"][2]["action"], "RESCOPE");
    assert_eq!(
        keeper.1.borrow().as_slice(),
        &[(
            "close_merged_auto".to_owned(),
            json!({"apply":false,"watch":true,"require_seats":true}),
        )]
    );
}

#[test]
fn hourly_reconcile_unit_invokes_only_the_dry_run() {
    let service = include_str!("../../../deploy/systemd/pg-reconcile-merged.service");
    let timer = include_str!("../../../deploy/systemd/pg-reconcile-merged.timer");
    assert!(service.contains("Type=oneshot"));
    assert!(service.contains(" reconcile-merged --dry-run --state "));
    assert!(timer.contains("OnCalendar=hourly"));
    assert!(timer.contains("Unit=pg-reconcile-merged.service"));
}
