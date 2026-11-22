use serde_json::{json, Value};
use std::fs;
use std::process::Command;
use std::time::{SystemTime, UNIX_EPOCH};

#[test]
fn recent_attempt_rows_preserve_writer_seat_and_adhoc_identity() {
    let state = tempfile::tempdir().unwrap();
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs();
    for (id, role, item, started, status) in [
        (
            "run-writer",
            "implement",
            Some("demo:WRITE"),
            now - 90,
            "completed",
        ),
        (
            "run-seat",
            "review",
            Some("demo:WRITE"),
            now - 60,
            "completed",
        ),
        ("run-adhoc", "review", None, now - 30, "failed"),
        ("run-dry", "review", None, now - 30, "dry_run"),
        ("run-old", "implement", None, now - 90_000, "completed"),
    ] {
        let path = state.path().join("runs").join(id);
        fs::create_dir_all(&path).unwrap();
        let leg = json!({"id":"leg-a","model":"request","harness":"codex","effort":"high"});
        let record = json!({
            "run_id": id, "role": role, "item_key":item, "host":"test-host",
            "started":started, "ended":started + 15, "status":status,
            "plan":{
                "legs":[leg], "candidates":[{"leg":"leg-a","u_core":0.7,"p_source":"measured",
                    "intelligence":49.0,"cost_per_task":0.5}],
                "static_v0":"leg-a",
                "skipped":[{"leg":"leg-b","reason":"unavailable"}],
                "effort":{"level":"L2","reason":"bucket","task_class":"medium_change","risk_band":"normal"}
            },
            "attempts":[
                {"job_id":"job-a","leg":leg,"status":status,"served_model":"witnessed",
                 "usage":{"input_tokens":12,"output_tokens":3}},
                {"leg":"leg-b","status":"skipped"}
            ]
        });
        fs::write(path.join("record.json"), record.to_string()).unwrap();
    }
    let output = Command::new(env!("CARGO_BIN_EXE_pg-spawnd"))
        .args(["attempts", "--since", "1d", "--json", "--state"])
        .arg(state.path())
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let rows: Vec<Value> = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(rows.len(), 3);
    for row in &rows {
        assert_eq!(row["host"], "test-host");
        assert_eq!(row["harness"], "codex");
        assert_eq!(row["requested_model"], "request");
        assert_eq!(row["served_model"], "witnessed");
        assert_eq!(row["level"], "L2");
        assert_eq!(row["task_class"], "medium_change");
        assert_eq!(row["route"]["eligible_n"], 1);
        assert_eq!(row["route"]["rejected"][0]["leg"], "leg-b");
        assert_eq!(row["route"]["top5"][0]["effort"], "high");
        assert_eq!(
            row["route"]["baselines"],
            json!({
                "static_v0":"leg-a", "strongest":"leg-a", "cheapest":"leg-a"
            })
        );
        assert_eq!(row["route"]["chosen"], "leg-a");
        assert_eq!(row["tokens_in"], 12);
        assert_eq!(row["wall_seconds"], 15.0);
        assert!(row["attempt_id"].is_string());
        assert!(row["plan_hash"].is_string());
    }
    assert_eq!(rows[0]["card_key"], "demo:WRITE");
    assert_eq!(rows[0]["role"], "implement");
    assert_eq!(rows[1]["role"], "review");
    assert_eq!(rows[2]["card_key"], "adhoc");
}
