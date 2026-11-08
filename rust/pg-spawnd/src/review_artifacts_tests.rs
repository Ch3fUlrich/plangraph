use super::*;
#[test]
fn actual_seat_verdict_dry_run_reads_pair() {
    let state = std::env::temp_dir().join(pg_worker_core::run::record::new_run_id());
    let base = json!({"run_id":"run-test","served_model":"gpt-5","witness_kind":"response",
        "finish_reason":"stop","test_gaming":"no","verdict":"PASS"});
    for (body, pass) in [
        ("analysis\nVERDICT: HALT", false),
        ("analysis\nverdict: halt", false),
        ("", false),
        ("  \n", false),
        ("analysis only", false),
        ("VERDICT: pass | fix", false),
        ("quoted \"VERDICT: HALT\"\n**Verdict**: ACCEPT", false),
        ("analysis\nVERDICT: PASS", true),
        ("approve", false),
        ("VERDICT: pass - fix", false),
        ("verdict: pass-through", false),
        ("verdict: accept-all", false),
        ("VERDICT: PASS\nVERDICT: unknown", false),
        ("VERDICT: ACCEPT\nVERDICT: unknown", false),
    ] {
        write_review_artifacts(&state, &base, body).unwrap();
        let seat = state.join("runs/run-test/seat");
        let evidence: Value =
            serde_json::from_slice(&fs::read(seat.join("evidence.json")).unwrap()).unwrap();
        assert_eq!(
            evidence["verdict"],
            if pass { "PASS" } else { "HALT" },
            "{body}"
        );
        assert_eq!(fs::read_to_string(seat.join("review.txt")).unwrap(), body);
        if pass || body.contains("quoted") || body == "analysis\nVERDICT: HALT" {
            let output = std::process::Command::new("python3")
                .arg(concat!(
                    env!("CARGO_MANIFEST_DIR"),
                    "/../../tools/seat_verdict.py"
                ))
                .args([
                    "--dry-run",
                    "--key",
                    "test",
                    "--writer-family",
                    "meta",
                    "--token-dir",
                ])
                .arg(&state)
                .arg("--seat-dir")
                .arg(&seat)
                .output()
                .unwrap();
            assert!(
                output.status.success(),
                "{}",
                String::from_utf8_lossy(&output.stderr)
            );
            let payload: Value = serde_json::from_slice(&output.stdout).unwrap();
            assert_eq!(
                payload["body"]["ok"],
                pass || body.contains("quoted"),
                "{body}"
            );
        }
    }
    let mut incomplete = base;
    incomplete["finish_reason"] = Value::Null;
    write_review_artifacts(&state, &incomplete, "analysis\nVERDICT: HALT").unwrap();
    assert_eq!(
        fs::read_to_string(state.join("runs/run-test/seat/review.txt")).unwrap(),
        "analysis\nVERDICT: HALT"
    );
    fs::remove_dir_all(state).unwrap();
}
#[test]
fn missing_witness_or_finish_never_pass() {
    let state = std::env::temp_dir().join(pg_worker_core::run::record::new_run_id());
    let base = json!({"run_id":"run-test","served_model":"model","witness_kind":"response",
        "finish_reason":"stop","test_gaming":false,"verdict":"PASS"});
    for key in [
        "finish_reason",
        "test_gaming",
        "served_model",
        "witness_kind",
    ] {
        for missing in [Value::Null, json!("unsure")] {
            let mut record = base.clone();
            record[key] = missing;
            write_review_artifacts(&state, &record, "VERDICT: PASS\nnotes").unwrap();
            let seat = state.join("runs/run-test/seat");
            assert_eq!(
                fs::read_to_string(seat.join("review.txt")).unwrap(),
                "VERDICT: PASS\nnotes"
            );
            let evidence: Value =
                serde_json::from_slice(&fs::read(seat.join("evidence.json")).unwrap()).unwrap();
            assert_eq!(evidence["verdict"], "HALT");
        }
    }
    let mut incomplete = base.clone();
    incomplete["finish_reason"] = Value::Null;
    write_review_artifacts(&state, &incomplete, "VERDICT: ACCEPT\nnotes").unwrap();
    assert_eq!(
        fs::read_to_string(state.join("runs/run-test/seat/review.txt")).unwrap(),
        "VERDICT: ACCEPT\nnotes"
    );
    write_review_artifacts(&state, &base, "VERDICT: ACCEPT").unwrap();
    assert_eq!(
        fs::read_to_string(state.join("runs/run-test/seat/review.txt")).unwrap(),
        "VERDICT: ACCEPT"
    );
    fs::remove_dir_all(state).unwrap();
}
#[test]
fn full_text_not_tail_and_unknown_format_nonpassing() {
    let state = std::env::temp_dir().join(pg_worker_core::run::record::new_run_id());
    let dir = state.join("runs/run-test");
    fs::create_dir_all(&dir).unwrap();
    let path = dir.join("live.log");
    let record = json!({"run_id":"run-test","live_log":path,"stdout_tail":"VERDICT: ACCEPT"});
    let full = format!("{}\nVERDICT: ACCEPT", "detail ".repeat(10000));
    fs::write(
        &path,
        format!(
            "{}\n",
            json!({"type":"result","is_error":false,"result":full})
        ),
    )
    .unwrap();
    assert_eq!(review_text(&state, &record).unwrap(), full);
    fs::write(&path, "unknown\n").unwrap();
    assert!(review_text(&state, &record)
        .unwrap()
        .contains("VERDICT: HALT"));
    fs::remove_dir_all(state).unwrap();
}
#[test]
fn traversal_run_id_refused() {
    assert!(write_review_artifacts(
        Path::new("unused"),
        &json!({"run_id":"../escape"}),
        "VERDICT: ACCEPT"
    )
    .is_err());
}
#[test]
fn switch_off_still_artifacts() {
    let state = std::env::temp_dir().join(pg_worker_core::run::record::new_run_id());
    let mut record = crate::runs::create(
        &state,
        "run-test",
        "local",
        json!({"role":"review"}),
        pg_worker_core::run::record::Status::Completed,
    )
    .ok()
    .unwrap();
    let log = record.path().with_file_name("live.log");
    std::fs::write(&log, [0xff, b'\n']).unwrap();
    record.set("live_log", json!(log));
    assert!(crate::review_artifacts::review_text(&state, &record.to_value()).is_err());
    crate::runs::finish(
        &mut record,
        pg_worker_core::run::record::Status::Completed,
        &json!({}),
        1,
    );
    assert!(!state.join("evidence").exists());
    assert_eq!(record.to_value()["evidence_unclassified_attempts"], 1);
    let seat = record.path().with_file_name("seat");
    assert!(std::fs::read_to_string(seat.join("review.txt"))
        .unwrap()
        .contains("VERDICT: HALT"));
    let evidence: Value =
        serde_json::from_slice(&std::fs::read(seat.join("evidence.json")).unwrap()).unwrap();
    assert_eq!(evidence["verdict"], "HALT");
    std::fs::remove_dir_all(state).unwrap();
}
