use super::*;
#[test]
fn health_json_and_report_flags_help() {
    for args in [vec!["--help"], vec!["report", "--help"]] {
        let mut out = Vec::new();
        execute(args.into_iter().map(String::from).collect(), &mut out).unwrap();
        assert!(String::from_utf8(out).unwrap().contains("--baseline"));
    }
    assert_eq!(timestamp("1970-01-01T00:00:00Z").unwrap(), 0);
    assert_eq!(timestamp("1969-12-31T23:00:00-01:00").unwrap(), 0);
    assert!(timestamp("2024-02-29T00:00:00Z").is_ok());
    assert_eq!(timestamp("1970-01-01T01:00:00+01:00").unwrap(), 0);
    assert_eq!(
        timestamp("2026-11-02T10:30:00Z").unwrap() - timestamp("2026-10-30T10:30:00Z").unwrap(),
        259200
    );
}
#[test]
fn invalid_windows_and_duplicates() {
    for s in [
        "2026-02-29T00:00:00Z",
        "2026-01-01T24:00:00Z",
        "2026-01-01T00:00:00",
        "bad",
        "2026-01-01T00:00:00.1Z",
        "2026-01-01T00:00:00+24:00",
        "2026-01-01T00:00:60Z",
    ] {
        assert!(timestamp(s).is_err());
    }
    for args in [
        vec!["--json", "--json"],
        vec!["report", "--since"],
        vec!["--unknown"],
    ] {
        assert!(execute(
            args.into_iter().map(String::from).collect(),
            &mut Vec::new()
        )
        .is_err());
    }
}
#[test]
fn baseline_stored_private_not_symlink() {
    let dir = std::env::temp_dir().join(pg_worker_core::run::record::new_run_id());
    std::fs::create_dir(&dir).unwrap();
    baseline(&dir, 0, 1, &serde_json::json!({"version":1})).unwrap();
    let path = dir.join("reports/baseline-0-1.json");
    assert!(path.is_file());
    #[cfg(unix)]
    {
        use std::os::unix::fs::{symlink, PermissionsExt};
        assert_eq!(
            std::fs::metadata(&path).unwrap().permissions().mode() & 0o777,
            0o600
        );
        symlink(&path, dir.join("reports/baseline-1-2.json")).unwrap();
        assert!(baseline(&dir, 1, 2, &Value::Null).is_err());
    }
    std::fs::remove_dir_all(dir).unwrap();
}

#[test]
fn equal_and_inverted_cli_windows() {
    let dir = std::env::temp_dir().join(pg_worker_core::run::record::new_run_id());
    std::fs::create_dir_all(dir.join("evidence")).unwrap();
    let file = std::fs::File::create(dir.join("evidence/outcomes-2026-01.jsonl")).unwrap();
    file.set_len(16 * 1024 * 1024 + 1).unwrap();
    for until in ["2026-01-02T00:00:00Z", "2026-01-01T00:00:00Z"] {
        let error = execute(
            [
                "report",
                "--state",
                dir.to_str().unwrap(),
                "--since",
                "2026-01-02T00:00:00Z",
                "--until",
                until,
            ]
            .into_iter()
            .map(String::from)
            .collect(),
            &mut Vec::new(),
        )
        .unwrap_err();
        assert_eq!(error, "health report: InvalidWindow");
    }
    std::fs::remove_dir_all(dir).unwrap();
}

#[test]
fn report_prints_one_witness_collapse_alert_without_breaking_json() {
    let dir = std::env::temp_dir().join(pg_worker_core::run::record::new_run_id());
    let until = timestamp("1970-01-03T00:00:00Z").unwrap();
    for index in 0..18 {
        let id = format!("run-{index:032x}");
        let path = dir.join("runs").join(&id);
        std::fs::create_dir_all(&path).unwrap();
        let record = serde_json::json!({"run_id":id, "status":"completed",
            "ended":until - if index < 10 { 32400 } else { 10800 },
            "chosen_leg":{"id":"mimo-fixture"},
            "served_model":if index < 9 {"mimo/mimo-v2.6-pro"} else {"unwitnessed"},
            "witness_kind":if index < 9 {"response-side-gateway-call-log"} else {""}});
        std::fs::write(path.join("record.json"), record.to_string()).unwrap();
    }
    let args: Vec<String> = [
        "report",
        "--state",
        dir.to_str().unwrap(),
        "--since",
        "1970-01-02T12:00:00Z",
        "--until",
        "1970-01-03T00:00:00Z",
    ]
    .into_iter()
    .map(String::from)
    .collect();
    let mut text = Vec::new();
    execute(args.clone(), &mut text).unwrap();
    assert_eq!(
        String::from_utf8(text)
            .unwrap()
            .lines()
            .filter(|line| line.starts_with("ALERT "))
            .count(),
        1
    );
    let mut args = args;
    args.push("--json".into());
    let mut text = Vec::new();
    execute(args, &mut text).unwrap();
    let report: Value = serde_json::from_slice(&text).unwrap();
    assert_eq!(report["witness_alerts"].as_array().unwrap().len(), 1);
    std::fs::remove_dir_all(dir).unwrap();
}

#[test]
fn review_acceptance_report_uses_serving_leg_and_replays_alarm() {
    let state = tempfile::tempdir().unwrap();
    let start = timestamp("2026-11-24T12:00:00Z").unwrap();
    let save = |id: &str, record: Value| {
        let dir = state.path().join("runs").join(id);
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("record.json"), record.to_string()).unwrap();
    };
    for (seat, passes) in [("strict", 1), ("control", 8)] {
        for i in 0..20 {
            let id = format!("run-{seat}{i}");
            let head = format!("{:040x}", 2 * i + u64::from(seat == "strict"));
            let ended = start + 600 + i as i64 * 60;
            let pass = if seat == "control" {
                i >= 20 - passes
            } else {
                i < passes
            };
            save(
                &id,
                serde_json::json!({"run_id":id, "role":"review","head_sha":head,
                    "status":"completed","started":ended - 20,"ended":ended,
                    "review_result":"verdict", "chosen_leg":{"id":"first"},
                    "attempts":[{"status":"limited","leg":{"id":"first"}},
                                {"status":"completed","leg":{"id":seat}},
                                {"status":"skipped","leg":{"id":"never-served"}}],
                    "stdout_tail":if pass {"VERDICT: ACCEPT"} else {"VERDICT: REQUEST_CHANGES"}}),
            );
            let writer = format!("run-writer{seat}{i}");
            save(
                &writer,
                serde_json::json!({"run_id":writer,"role":"implement","run_kind":"native-codex",
                    "review_rounds":2,"reviews":[{"run_id":"unjoined","head":"old",
                        "verdict":"REQUEST_CHANGES"},{"run_id":id,"head":head,"leg":seat,
                        "verdict":if pass {"ACCEPT"} else {"REQUEST_CHANGES"},"accepted_at":if pass {Some(ended)} else {None}}]}),
            );
        }
    }
    save(
        "run-legacy",
        serde_json::json!({
            "run_id":"run-legacy","role":"review","status":"completed",
            "started":start,"ended":start+1,"review_result":"verdict","chosen_leg":{"id":"strict"}
        }),
    );
    for (id, verdict) in [
        ("run-unadmitted-pass", "ACCEPT"),
        ("run-unadmitted-changes", "REQUEST_CHANGES"),
    ] {
        save(
            id,
            serde_json::json!({
                "run_id":id,"role":"review","status":"completed","head_sha":"a".repeat(40),
                "started":start,"ended":start+2,"stdout_tail":format!("VERDICT: {verdict}"),
                "chosen_leg":{"id":"strict"}
            }),
        );
    }
    save(
        "run-active",
        serde_json::json!({
            "run_id":"run-active","role":"review","status":"running","started":start+3*3600,
            "attempts":[{"leg":{"id":"strict"},"status":"running"}]
        }),
    );
    save(
        "run-queued",
        serde_json::json!({
            "run_id":"run-queued","role":"review","status":"queued","started":start+6*3600
        }),
    );
    let report = |since, until| {
        let args = [
            "report",
            "--state",
            state.path().to_str().unwrap(),
            "--since",
            since,
            "--until",
            until,
            "--json",
        ];
        let mut out = Vec::new();
        execute(args.map(String::from).to_vec(), &mut out).unwrap();
        serde_json::from_slice::<Value>(&out).unwrap()["review_acceptance"].clone()
    };
    let day = report("2026-11-24T12:00:00Z", "2026-11-24T21:00:00Z");
    let first = &day["windows_3h"][0]["seats"];
    assert_eq!(first["strict"]["verdicts"], 20);
    assert_eq!(first["strict"]["accepts"], 1);
    assert_eq!(first["strict"]["median_rounds_to_accept"], 2.0);
    assert_eq!(first["control"]["accepts"], 8);
    assert_eq!(first["control"]["verdicts"], 20);
    assert_eq!(day["days"][0]["seats"]["strict"]["verdicts"], 20);
    let alerts = day["alerts"].as_array().unwrap();
    assert_eq!(
        alerts
            .iter()
            .filter(|a| a["kind"] == "low_acceptance")
            .count(),
        1
    );
    assert!(alerts.iter().any(|a| a["kind"] == "low_acceptance"
        && a["leg"] == "strict"
        && a["at"].as_f64() == Some((start + 600 + 9 * 60) as f64)));
    assert_eq!(first["strict"]["unknown_verdicts"], 2);
    assert!(!alerts.iter().any(|a| a["kind"] == "no_head_accepted"));
    assert_eq!(day["coverage"]["complete"], false);
    assert_eq!(day["coverage"]["scope"], "local");
}

#[test]
fn local_health_report_cannot_establish_fleet_silence() {
    let state = tempfile::tempdir().unwrap();
    let start = timestamp("2026-11-24T15:00:00Z").unwrap();
    let path = state.path().join("runs/run-active");
    std::fs::create_dir_all(&path).unwrap();
    std::fs::write(
        path.join("record.json"),
        serde_json::json!({
            "run_id":"run-active","role":"review","status":"running","started":start,
            "attempts":[{"leg":{"id":"strict"},"status":"running"}]
        })
        .to_string(),
    )
    .unwrap();
    let args = [
        "report",
        "--state",
        state.path().to_str().unwrap(),
        "--since",
        "2026-11-24T15:00:00Z",
        "--until",
        "2026-11-24T17:00:01Z",
        "--json",
    ];
    let mut out = Vec::new();
    execute(args.map(String::from).to_vec(), &mut out).unwrap();
    let report: Value = serde_json::from_slice(&out).unwrap();
    assert!(report["review_acceptance"]["alerts"]
        .as_array()
        .unwrap()
        .is_empty());
    assert_eq!(report["review_acceptance"]["coverage"]["complete"], false);
}
