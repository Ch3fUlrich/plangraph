use super::*;

#[test]
fn a_return_releases_each_receipt_item_at_its_saved_epoch_before_it_reports() {
    let dir = tempfile::tempdir().unwrap();
    // The ledger key names no claim; the receipts of the card do.
    assert!(enqueue_return(
        dir.path(),
        "p:Batch",
        "round_cap",
        &[],
        Some(&claims(&[("p:One", 3), ("p:Two", 7)]))
    )
    .ok()
    .unwrap());
    let ports = Fixture::default();
    ports.claimed.borrow_mut().insert("p:One".into(), 3);
    ports.claimed.borrow_mut().insert("p:Two".into(), 7);
    *ports.fail_release.borrow_mut() = true;
    assert!(run_pending(dir.path(), &ports)[0].1.contains("keeper_down"));
    assert_eq!(ports.count("route"), 0);
    *ports.fail_release.borrow_mut() = false;
    assert_eq!(run_pending(dir.path(), &ports)[0].1, "done");
    assert_eq!(run_pending(dir.path(), &ports)[0].1, "done");
    let calls = ports.calls.borrow().clone();
    assert!(calls.contains(&"release p:One at 3".to_string()));
    assert!(calls.contains(&"release p:Two at 7".to_string()));
    assert!(!calls
        .iter()
        .any(|c| c.starts_with("release") && c.contains("p:Batch")));
    let last_release = calls
        .iter()
        .rposition(|c| c.starts_with("release"))
        .unwrap();
    let route = calls.iter().position(|c| c.starts_with("route")).unwrap();
    assert!(last_release < route);
    assert_eq!(calls.iter().filter(|c| c.starts_with("route")).count(), 1);
}

#[test]
fn a_replaced_claim_is_never_released_and_a_committed_release_is_reconciled() {
    // The claim of the receipt (epoch 3) was replaced by epoch 5 before the job ran.
    let dir = tempfile::tempdir().unwrap();
    enqueue_return(
        dir.path(),
        "p:I",
        "round_cap",
        &[],
        Some(&claims(&[("p:I", 3)])),
    )
    .ok()
    .unwrap();
    let ports = Fixture::default();
    ports.claimed.borrow_mut().insert("p:I".into(), 5);
    assert_eq!(run_pending(dir.path(), &ports)[0].1, "done");
    assert_eq!(ports.claimed.borrow().get("p:I"), Some(&5));
    assert_eq!(ports.count("release p:I at 5"), 0);
    // A release that went through and lost its answer: the retry finds the epoch gone.
    let dir = tempfile::tempdir().unwrap();
    enqueue_return(
        dir.path(),
        "p:I",
        "round_cap",
        &[],
        Some(&claims(&[("p:I", 3)])),
    )
    .ok()
    .unwrap();
    let ports = Fixture::default();
    ports.claimed.borrow_mut().insert("p:I".into(), 3);
    *ports.crash_after_release.borrow_mut() = true;
    assert_eq!(run_pending(dir.path(), &ports)[0].1, "done");
    assert_eq!(ports.count("release p:I at 3"), 1);
    assert_eq!(ports.count("route"), 1);
}

#[test]
fn a_return_without_saved_receipts_stays_pending_with_an_explicit_error() {
    // A job of the previous head (claim_items, no bound receipts) and a card without claims.
    let dir = tempfile::tempdir().unwrap();
    std::fs::create_dir_all(job_dir(dir.path())).unwrap();
    std::fs::write(
        job_dir(dir.path()).join("replan-prev.json"),
        json!({"item":"p:I", "pr":"", "reason":"round_cap", "kind":"return", "queued_at":5.0,
        "claim_items":["p:I"], "claims":{}, "plan":{"leg":null, "slices":[], "findings":[]},
        "defined":[], "closed":false, "report":"pending", "error":null})
        .to_string(),
    )
    .unwrap();
    enqueue_return(dir.path(), "p:J", "round_cap", &[], None)
        .ok()
        .unwrap();
    let ports = Fixture::default();
    ports.claimed.borrow_mut().insert("p:I".into(), 3);
    for (_, outcome) in run_pending(dir.path(), &ports) {
        assert!(outcome.contains("replan_claims_unbound"), "{outcome}");
    }
    assert_eq!(ports.count("route"), 0);
    assert_eq!(ports.count("release"), 0);
    assert_eq!(ports.claimed.borrow().get("p:I"), Some(&3));
    assert!(jobs(dir.path()).iter().all(|j| j["released"] != true));
}

#[test]
fn a_saved_line_is_replayed_unchanged_and_only_a_refusal_for_size_shortens_it() {
    let saved = |dir: &Path, name: &str, text: &str| -> String {
        std::fs::create_dir_all(job_dir(dir)).unwrap();
        let job = json!({"item":"p:Old", "pr":"https://forge/p/1", "reason":"escalated",
            "level":1, "queued_at":5.0, "plan":{"leg":"strong", "slices":[]}, "defined":[],
            "closed":false, "report":"sending", "report_line":text, "error":null});
        std::fs::write(job_dir(dir).join(name), job.to_string()).unwrap();
        report_key(&job)
    };
    // A 3,786-byte body the bus took before its ack was lost: replayed byte for byte, once.
    let deliverable = format!("REPLAN p:Old {}", "x".repeat(3_700));
    let dir = tempfile::tempdir().unwrap();
    let key = saved(dir.path(), "replan-ok.json", &deliverable);
    let ports = Fixture::default();
    *ports.overhead.borrow_mut() = 300;
    ports
        .taken
        .borrow_mut()
        .insert(key.clone(), deliverable.clone());
    assert_eq!(run_pending(dir.path(), &ports)[0].1, "done");
    assert_eq!(ports.taken.borrow().get(&key), Some(&deliverable));
    // The same job against a bus with long identifiers (4,4xx-byte envelope) is refused for
    // size, then shortened until the bus takes it; the worker run twice delivers once.
    let dir = tempfile::tempdir().unwrap();
    saved(
        dir.path(),
        "replan-old.json",
        &format!("REPLAN p:Old {}", "x".repeat(3_000)),
    );
    let ports = Fixture::default();
    *ports.overhead.borrow_mut() = 2_500;
    assert_eq!(run_pending(dir.path(), &ports)[0].1, "done");
    assert_eq!(run_pending(dir.path(), &ports)[0].1, "done");
    let sent = ports.calls.borrow().last().unwrap().clone();
    assert!(sent.contains("(full text in replans/replan-old.json)"));
    assert!(wire(sent.trim_start_matches("route ")) + 2_500 <= 4096);
    assert_eq!(ports.count("route"), 1);
    assert!(ports.sent_keys.borrow().len() >= 3);
    // A line of DEL characters costs six bytes each; it is shortened the same way.
    let dir = tempfile::tempdir().unwrap();
    saved(
        dir.path(),
        "replan-del.json",
        &format!("REPLAN p:Old {}", "\u{7f}".repeat(800)),
    );
    let ports = Fixture::default();
    assert_eq!(run_pending(dir.path(), &ports)[0].1, "done");
    assert_eq!(ports.count("route"), 1);
    // A bus that refuses even the shortest line fails the job with its refusal, kept pending.
    let dir = tempfile::tempdir().unwrap();
    saved(
        dir.path(),
        "replan-tiny.json",
        &format!("REPLAN p:Old {}", "x".repeat(900)),
    );
    let ports = Fixture::default();
    *ports.overhead.borrow_mut() = 4_000;
    assert!(run_pending(dir.path(), &ports)[0].1.contains(TOO_LARGE));
    assert_eq!(ports.count("route"), 0);
}

#[test]
fn claim_receipts_are_validated_whole_and_a_later_retry_attaches_them() {
    use crate::round_escalate::receipts;
    assert!(receipts(&json!({"p:One":3, "p:Two":null})).is_some());
    for bad in [
        json!({"p:One":3, "p:Two":"x"}),
        json!({"p:One":3, "not a key":4}),
        json!([3]),
    ] {
        assert!(receipts(&bad).is_none());
    }
    // A return queued without receipts is pending; the validated retry attaches them under the lock.
    let dir = tempfile::tempdir().unwrap();
    assert!(
        enqueue_return(dir.path(), "p:Batch", "round_cap", &[], None)
            .ok()
            .unwrap()
    );
    let ports = Fixture::default();
    ports.claimed.borrow_mut().insert("p:One".into(), 3);
    assert!(run_pending(dir.path(), &ports)[0]
        .1
        .contains("replan_claims_unbound"));
    let given = receipts(&json!({"p:One":3})).unwrap();
    assert!(
        enqueue_return(dir.path(), "p:Batch", "round_cap", &[], Some(&given))
            .ok()
            .unwrap()
    );
    assert!(
        !enqueue_return(dir.path(), "p:Batch", "round_cap", &[], Some(&given))
            .ok()
            .unwrap()
    );
    assert_eq!(run_pending(dir.path(), &ports)[0].1, "done");
    assert_eq!(ports.claimed.borrow().get("p:One"), None);
    assert_eq!(ports.count("route"), 1);
    // A receipt map with a member that has no epoch releases nothing at all.
    let dir = tempfile::tempdir().unwrap();
    std::fs::create_dir_all(job_dir(dir.path())).unwrap();
    std::fs::write(
        job_dir(dir.path()).join("replan-half.json"),
        json!({"item":"p:B", "pr":"", "reason":"round_cap", "kind":"return", "queued_at":5.0,
        "claims_bound":true, "claims":{"p:One":{"epoch":3, "released":false},
        "p:Two":{"epoch":null, "released":false}},
        "plan":{"leg":null, "slices":[], "findings":[]}, "defined":[], "closed":false,
        "report":"pending", "error":null})
        .to_string(),
    )
    .unwrap();
    let ports = Fixture::default();
    ports.claimed.borrow_mut().insert("p:One".into(), 3);
    assert!(run_pending(dir.path(), &ports)[0]
        .1
        .contains("nothing was released"));
    assert_eq!(ports.claimed.borrow().get("p:One"), Some(&3));
}

#[test]
fn a_refusal_without_receipts_takes_them_from_the_keeper_and_releases_through_it() {
    use crate::round_replan_ports::{Exec, Tool};
    struct Keeper(std::cell::RefCell<Vec<String>>);
    impl Exec for Keeper {
        fn run(&self, tool: Tool, args: &[String]) -> Result<String, ToolError> {
            assert_eq!(tool, Tool::Keeper);
            self.0.borrow_mut().push(args.join(" "));
            let item = &args[2];
            Ok(match item.as_str() {
                "p:One" => json!({"state":"claimed", "epoch":3}),
                "p:Two" => json!({"state":"open", "epoch":9}),
                _ => return Err(parameter("replan_keeper_failed: unreachable")),
            }
            .to_string())
        }
    }
    let dir = tempfile::tempdir().unwrap();
    let mut args = crate::placement::Args::new();
    args.insert(
        "card".into(),
        json!({"item_key":"p:One", "item_keys":["p:One", "p:Two"]}),
    );
    let keeper = Keeper(Default::default());
    let refusal = crate::round_progress::record_refusal_with(
        dir.path(),
        "fixture",
        &args,
        crate::round_progress::refused("round_cap"),
        &keeper,
    )
    .ok()
    .unwrap();
    assert_eq!(refusal["replan"], "returned");
    assert!(keeper
        .0
        .borrow()
        .iter()
        .all(|c| c.ends_with("--authoritative")));
    let job = jobs(dir.path()).pop().unwrap();
    assert_eq!(job["claims_bound"], true);
    assert_eq!(job["claims"]["p:One"]["epoch"], 3);
    assert_eq!(job["claims"]["p:Two"]["released"], true);
    let ports = Fixture::default();
    ports.claimed.borrow_mut().insert("p:One".into(), 3);
    assert_eq!(run_pending(dir.path(), &ports)[0].1, "done");
    assert_eq!(ports.count("release p:One at 3"), 1);
    assert_eq!(ports.count("release p:Two"), 0);
    // An unreachable keeper binds nothing: the job is pending with the explicit error.
    let dir = tempfile::tempdir().unwrap();
    let mut args = crate::placement::Args::new();
    args.insert("card".into(), json!({"item_key":"p:Gone"}));
    crate::round_progress::record_refusal_with(
        dir.path(),
        "fixture",
        &args,
        crate::round_progress::refused("round_cap"),
        &Keeper(Default::default()),
    )
    .ok()
    .unwrap();
    assert_eq!(jobs(dir.path())[0]["claims_bound"], false);
}
