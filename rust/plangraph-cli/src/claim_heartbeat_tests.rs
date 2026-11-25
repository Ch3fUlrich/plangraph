use super::*;
use pg_worker_core::lane_roster::{claude_lanes, native_lanes};
use std::fs;

struct Fake {
    items: Value,
    refuse: Vec<&'static str>,
    sent: Vec<(String, i64)>,
    fail_batch: bool,
}

impl Keeper for Fake {
    fn principal(&mut self) -> Result<String, String> {
        Ok("plan-order".into())
    }
    fn claims(&mut self) -> Result<Vec<Claim>, String> {
        Ok(claims_from_snapshot(&json!({"items": self.items})))
    }
    fn heartbeat(&mut self, claims: &[(String, i64)]) -> Result<Vec<Value>, String> {
        self.sent.extend(claims.iter().cloned());
        if self.fail_batch {
            return Err("keeper unreachable".into());
        }
        Ok(claims
            .iter()
            .map(|(key, epoch)| {
                if self.refuse.contains(&key.as_str()) {
                    json!({"key":key,"error":"stale_epoch","msg":"epoch moved"})
                } else {
                    json!({"key":key,"epoch":epoch,"lease_until":"T1"})
                }
            })
            .collect())
    }
}

fn item(key: &str, by: &str, merged: bool) -> Value {
    json!({"key":key,"state":"claimed","epoch":4,"claimed_by":by,"merged":merged})
}

#[test]
fn the_roster_decides_what_is_renewed_and_failures_carry_their_keys() {
    let dir = tempfile::tempdir().unwrap();
    // A live native run, a finished run, a Claude writer session and an open pull request.
    for (id, status, key) in [
        ("run-live", "running", "p:NATIVE"),
        ("run-done", "completed", "p:DONE"),
    ] {
        let run = dir.path().join("state/runs").join(id);
        fs::create_dir_all(&run).unwrap();
        let record = json!({"run_id":id,"run_kind":"native-codex","status":status});
        fs::write(run.join("record.json"), record.to_string()).unwrap();
        let request = json!({"prompt":format!("Item key: {key}\nNAME: {id}\n")});
        fs::write(run.join("native-request.json"), request.to_string()).unwrap();
    }
    let sessions = dir.path().join("sessions");
    fs::create_dir_all(&sessions).unwrap();
    let brief = dir.path().join("brief");
    fs::write(&brief, "## p:CLAUDE | t\nNAME: claude-lane\n").unwrap();
    let record = json!({"name":"writer","session_name":"writer-0001","prompt_file":brief});
    fs::write(sessions.join("writer-0001.json"), record.to_string()).unwrap();
    let mut lanes = native_lanes(&dir.path().join("state"));
    lanes.extend(claude_lanes(&sessions, &|_| None, "here"));
    lanes.extend(pull_request_lanes(
        r#"[{"number":9,"headRefName":"lane/x","title":"x","body":"Plan-Item: p:PR\nPlan-Item: p:REFUSED"},
            {"number":10,"headRefName":"lane/branchonly","title":"x","body":""}]"#,
    ));

    let mut keeper = Fake {
        items: json!([
            item("p:NATIVE", "plan-order", false),
            item("p:CLAUDE", "plan-order", false),
            item("p:PR", "plan-order", false),
            item("p:REFUSED", "plan-order", false),
            item("p:DONE", "plan-order", false),
            item("p:OTHER", "someone-else", false),
            item("p:MERGED", "plan-order", true),
            item("p:BRANCHONLY", "plan-order", false),
        ]),
        refuse: vec!["p:REFUSED"],
        sent: vec![],
        fail_batch: false,
    };
    let report = pass(&mut keeper, &lanes).unwrap();
    let sent: Vec<_> = keeper.sent.iter().map(|(k, _)| k.as_str()).collect();
    assert_eq!(
        sent,
        ["p:NATIVE", "p:CLAUDE", "p:PR", "p:REFUSED", "p:BRANCHONLY"]
    );
    assert_eq!((report.renewed, report.refused), (4, 1));
    for line in [
        "renewed p:NATIVE epoch 4 lease T1",
        "renewed p:CLAUDE epoch 4 lease T1",
        "renewed p:PR epoch 4 lease T1",
        "refused p:REFUSED stale_epoch: epoch moved",
        "skipped p:DONE no_live_lane",
        "skipped p:MERGED merged",
        "renewed p:BRANCHONLY epoch 4 lease T1",
        "claim-heartbeat: 4 renewed, 1 refused, 3 not renewed",
    ] {
        assert!(
            report.lines.contains(&line.to_owned()),
            "{line}\n{:#?}",
            report.lines
        );
    }
    assert!(!report.lines.iter().any(|l| l.contains("p:OTHER")));
}

#[test]
fn a_failed_batch_names_every_key_and_keeps_the_earlier_reports() {
    struct Flaky(Fake, usize);
    impl Keeper for Flaky {
        fn principal(&mut self) -> Result<String, String> {
            self.0.principal()
        }
        fn claims(&mut self) -> Result<Vec<Claim>, String> {
            self.0.claims()
        }
        fn heartbeat(&mut self, claims: &[(String, i64)]) -> Result<Vec<Value>, String> {
            self.1 += 1;
            self.0.fail_batch = self.1 > 1;
            self.0.heartbeat(claims)
        }
    }
    let keys: Vec<String> = (0..BATCH + 2).map(|n| format!("p:K{n}")).collect();
    let items: Vec<Value> = keys.iter().map(|k| item(k, "plan-order", false)).collect();
    let lane = Lane {
        name: "all".into(),
        kind: pg_worker_core::lane_roster::LaneKind::Native,
        detail: "run-x".into(),
        keys: keys.iter().cloned().collect(),
        branch: None,
    };
    let fake = Fake {
        items: json!(items),
        refuse: vec![],
        sent: vec![],
        fail_batch: false,
    };
    let report = pass(&mut Flaky(fake, 0), &[lane]).unwrap();
    assert_eq!((report.renewed, report.refused), (BATCH, 2));
    assert!(report.lines.contains(&format!(
        "refused p:K{BATCH} batch_error: keeper unreachable"
    )));
    assert!(report.lines.contains(&format!(
        "refused p:K{} batch_error: keeper unreachable",
        BATCH + 1
    )));
}

#[test]
fn a_claim_whose_close_pass_found_the_merge_is_not_renewed_even_with_a_live_lane() {
    let mut keeper = Fake {
        items: json!([
            {"key":"p:PENDING","state":"claimed","epoch":2,"claimed_by":"plan-order",
             "merged":false,"merge_proven":true},
            item("p:LIVE", "plan-order", false),
        ]),
        refuse: vec![],
        sent: vec![],
        fail_batch: false,
    };
    let lane = Lane {
        name: "lane".into(),
        kind: pg_worker_core::lane_roster::LaneKind::Native,
        detail: "run-x".into(),
        keys: ["p:PENDING".to_owned(), "p:LIVE".to_owned()].into(),
        branch: None,
    };
    let report = pass(&mut keeper, &[lane]).unwrap();
    assert_eq!(keeper.sent, [("p:LIVE".to_owned(), 4)]);
    assert!(
        report
            .lines
            .contains(&"skipped p:PENDING merged".to_owned()),
        "{:#?}",
        report.lines
    );
}
