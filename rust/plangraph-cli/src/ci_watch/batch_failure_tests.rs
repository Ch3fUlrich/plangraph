use super::batch_failure::*;
use super::main_freeze::Alert;
use super::Task;
use std::collections::BTreeMap;

fn task(pr: Option<u64>) -> Task {
    Task {
        key: "plangraph:CI-FIX-1".into(),
        head: "pr-7".into(),
        pr,
        run_id: 99,
        job: "rust".into(),
        test: "t::fails".into(),
        excerpt: "job log tail".into(),
        priority: 10,
        keeper_defined: false,
        keeper_key: None,
        ..Default::default()
    }
}

fn owners() -> Owners {
    Owners {
        prs: BTreeMap::from([("7".into(), "batch35".into())]),
        writers: BTreeMap::from([("batch35".into(), "writer-1".into())]),
        successors: BTreeMap::new(),
    }
}

#[derive(Default)]
struct Fake {
    reachable: Vec<String>,
    sent: Vec<(String, String)>,
}

impl Deliver for Fake {
    fn send(&mut self, writer: &str, record: &FailureRecord) -> Result<(), String> {
        if !self.reachable.iter().any(|w| w == writer) {
            return Err("unreachable".into());
        }
        assert_eq!(
            record.excerpt, "job log tail",
            "the job log travels with the failure"
        );
        self.sent.push((writer.into(), record.id.clone()));
        Ok(())
    }
}

#[test]
fn a_failure_of_a_batch_head_goes_to_the_batch_writer_and_creates_no_fix_lane() {
    let mut records = vec![];
    let mut fake = Fake {
        reachable: vec!["writer-1".into()],
        ..Fake::default()
    };
    let id = route(
        &mut records,
        &owners(),
        &task(Some(7)),
        100,
        &mut fake,
        |_| Ok(()),
    )
    .unwrap();
    assert!(id.is_some(), "handled by the batch path");
    assert_eq!(fake.sent.len(), 1);
    assert_eq!(records[0].delivered_to.as_deref(), Some("writer-1"));
    // Seen again: one record, one delivery.
    route(
        &mut records,
        &owners(),
        &task(Some(7)),
        200,
        &mut fake,
        |_| Ok(()),
    )
    .unwrap();
    assert_eq!((records.len(), fake.sent.len()), (1, 1));
}

#[test]
fn a_head_without_a_recorded_batch_stays_on_the_unchanged_path() {
    let mut records = vec![];
    let mut fake = Fake::default();
    assert_eq!(
        route(
            &mut records,
            &owners(),
            &task(Some(8)),
            1,
            &mut fake,
            |_| Ok(())
        )
        .unwrap(),
        None
    );
    assert_eq!(
        route(&mut records, &owners(), &task(None), 1, &mut fake, |_| Ok(
            ()
        ))
        .unwrap(),
        None
    );
    assert!(records.is_empty());
}

#[test]
fn a_stale_writer_id_reaches_the_successor_that_holds_the_batch() {
    let mut o = owners();
    o.successors.insert("writer-1".into(), "writer-2".into());
    assert_eq!(o.writer_of("batch35").as_deref(), Some("writer-2"));
    let mut records = vec![];
    let mut fake = Fake {
        reachable: vec!["writer-2".into()],
        ..Fake::default()
    };
    route(&mut records, &o, &task(Some(7)), 100, &mut fake, |_| Ok(())).unwrap();
    assert_eq!(fake.sent[0].0, "writer-2");
}

#[test]
fn a_record_is_saved_before_delivery_and_survives_an_unreachable_writer_until_escalation() {
    let mut records = vec![];
    let mut fake = Fake::default();
    let mut saved_before_send = false;
    route(
        &mut records,
        &owners(),
        &task(Some(7)),
        100,
        &mut fake,
        |r| {
            saved_before_send |= r.iter().all(|x| x.delivered_to.is_none());
            Ok(())
        },
    )
    .unwrap();
    assert!(saved_before_send && records.len() == 1 && records[0].delivered_to.is_none());
    let mut alerts: BTreeMap<String, Alert> = BTreeMap::new();
    sweep(
        &mut records,
        &owners(),
        100 + ESCALATE_SECS - 1,
        &mut fake,
        &mut alerts,
    );
    assert!(alerts.is_empty());
    let out = sweep(
        &mut records,
        &owners(),
        100 + ESCALATE_SECS,
        &mut fake,
        &mut alerts,
    );
    assert!(out[0].contains("plan-order"));
    let alert = alerts.values().next().unwrap();
    assert_eq!(alert.due, "plan-order");
    assert!(
        fake.sent.is_empty(),
        "the watcher launches and delivers nothing to nobody"
    );
    // Escalated once.
    assert!(sweep(
        &mut records,
        &owners(),
        100 + 2 * ESCALATE_SECS,
        &mut fake,
        &mut alerts
    )
    .is_empty());
}

#[test]
fn an_acknowledged_failure_is_neither_resent_nor_escalated() {
    let mut records = vec![];
    let mut fake = Fake {
        reachable: vec!["writer-1".into()],
        ..Fake::default()
    };
    let id = route(
        &mut records,
        &owners(),
        &task(Some(7)),
        100,
        &mut fake,
        |_| Ok(()),
    )
    .unwrap()
    .unwrap();
    assert!(acknowledge(&mut records, &id));
    let mut alerts = BTreeMap::new();
    sweep(
        &mut records,
        &owners(),
        100 + 10 * ESCALATE_SECS,
        &mut fake,
        &mut alerts,
    );
    assert!(alerts.is_empty() && fake.sent.len() == 1);
}

#[test]
fn a_later_sweep_delivers_to_the_writer_that_became_reachable() {
    let mut records = vec![];
    let mut fake = Fake::default();
    route(
        &mut records,
        &owners(),
        &task(Some(7)),
        100,
        &mut fake,
        |_| Ok(()),
    )
    .unwrap();
    fake.reachable.push("writer-1".into());
    let mut alerts = BTreeMap::new();
    let out = sweep(&mut records, &owners(), 200, &mut fake, &mut alerts);
    assert!(out[0].starts_with("delivered"));
    assert!(alerts.is_empty());
}

#[test]
fn a_batch_failure_record_carries_the_class_job_and_where_to_start() {
    let mut classed = task(Some(7));
    classed.class = "configuration".into();
    classed.run_class = "mixed".into();
    classed.job_id = 31;
    let mut records = vec![];
    record_failure(&mut records, &owners(), &classed, 5).unwrap();
    let brief = &records[0].class_brief;
    assert!(
        brief.contains("Class: configuration (run: mixed)"),
        "{brief}"
    );
    assert!(
        brief.contains("Failed job id: 31") && brief.contains("workflow"),
        "{brief}"
    );
    let json = serde_json::to_string(&records[0]).unwrap();
    assert!(
        json.contains("class_brief"),
        "the delivery command reads the record: {json}"
    );
}

#[test]
fn two_jobs_that_failed_without_a_test_id_are_two_records() {
    let (mut a, mut b) = (task(Some(7)), task(Some(7)));
    a.test = "<job-failure>".into();
    a.job = "Rust workspace".into();
    a.job_id = 11;
    b.test = "<job-failure>".into();
    b.job = "Rust workspace".into();
    b.job_id = 12;
    let mut records = vec![];
    let first = record_failure(&mut records, &owners(), &a, 5).unwrap();
    let second = record_failure(&mut records, &owners(), &b, 5).unwrap();
    assert_ne!(first, second);
    assert_eq!(records.len(), 2);
    // A real test keeps its id whatever job names it.
    let (mut c, mut d) = (task(Some(7)), task(Some(7)));
    c.job_id = 1;
    d.job_id = 2;
    assert_eq!(
        record_failure(&mut vec![], &owners(), &c, 5),
        record_failure(&mut vec![], &owners(), &d, 5)
    );
}
