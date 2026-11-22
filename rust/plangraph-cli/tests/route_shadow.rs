use rusqlite::Connection;
use serde_json::{json, Value};
use std::process::Command;

#[test]
fn compares_fixed_baseline_and_benchmark_utility_with_observed_outcomes() {
    let tmp = tempfile::tempdir().unwrap();
    let catalog = tmp.path().join("catalog.db");
    Connection::open(&catalog)
        .unwrap()
        .execute_batch(
            "CREATE TABLE cells (model_id TEXT, effort TEXT, harness TEXT, score REAL,
                cost_per_task_usd REAL, legacy INTEGER);
             CREATE VIEW catalog_benchmark_rows AS SELECT * FROM cells;
             INSERT INTO cells VALUES ('a/model','high','codex',65,2,0),
                 ('b/model','high','codex',60,0,0), ('b/model','low','codex',80,0,0),
                 ('c/model','high','codex',90,0,0);",
        )
        .unwrap();

    let make = |id: u32, class: &str, item: &str, leg: &str, exit: &str, cost: Value| {
        json!({
            "attempt_id": format!("run-{id}"), "card_key": item, "role": "implement",
            "task_class": class, "leg": leg, "requested_model": format!("{leg}/model"),
            "harness": "codex", "effort": "high", "exit_class": exit, "cost": cost,
            "verified": id == 2 || id == 3,
            "started_at": format!("2026-11-22T12:{id:02}:00Z"),
            "route": {"eligible_n": 2, "pin": false, "top5": [
                {"leg":"a","effort":"high"}, {"leg":"b","effort":"high"}],
                "baselines": {"static_v0":"a"}}
        })
    };
    let mut pinned = make(5, "small_change", "demo:C", "c", "ok", json!(0.0));
    pinned["route"]["pin"] = json!(true);
    let attempts = tmp.path().join("attempts.json");
    std::fs::write(
        &attempts,
        json!([
            make(4, "medium_change", "demo:E", "a", "ok", Value::Null),
            make(3, "small_change", "demo:B", "b", "ok", json!(0.0)),
            make(2, "small_change", "demo:A", "a", "ok", json!(3.0)),
            make(1, "small_change", "demo:A", "a", "check_fail", json!(1.0)),
            pinned,
            make(6, "small_change", "demo:D", "b", "ok", Value::Null)
        ])
        .to_string(),
    )
    .unwrap();
    let run = |format| {
        Command::new(env!("CARGO_BIN_EXE_plangraph"))
            .args(["route-shadow", "--since", "2026-11-22", "--attempts"])
            .arg(&attempts)
            .arg("--catalog")
            .arg(&catalog)
            .args(["--sample-floor", "3", format])
            .output()
            .unwrap()
    };

    let output = run("--json");
    assert!(output.status.success(), "{:?}", output);
    let report: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(report["excluded_pinned"], 1);
    let small = &report["rows"][1];
    assert_eq!(small["task_class"], "small_change");
    assert_eq!(small["n"], 4);
    assert_eq!(small["below_sample_floor"], false);
    assert_eq!(small["fixed"]["leg"], "a");
    assert_eq!(small["fixed"]["observed_n"], 2);
    assert_eq!(small["fixed"]["first_pass_n"], 1);
    assert_eq!(small["fixed"]["first_pass_rate"], 0.0);
    assert_eq!(small["fixed"]["cost_per_verified_success_usd"], 4.0);
    assert_eq!(small["utility"]["leg"], "b");
    assert_eq!(small["utility"]["observed_n"], 2);
    assert_eq!(small["utility"]["first_pass_rate"], 1.0);
    assert_eq!(small["utility"]["verified_n"], 1);
    assert_eq!(
        small["utility"]["cost_per_verified_success_usd"],
        Value::Null
    );
    assert_eq!(report["rows"][0]["below_sample_floor"], true);

    let output = run("--text");
    assert!(output.status.success(), "{:?}", output);
    let table = String::from_utf8(output.stdout).unwrap();
    assert!(table.contains("medium_change"));
    assert!(table.contains("below floor"));
    assert!(table.contains("small_change"));
    assert!(table.contains("4.000"));
}

#[test]
fn keeps_partial_catalog_coverage_and_unknown_verification_visible() {
    let tmp = tempfile::tempdir().unwrap();
    let catalog = tmp.path().join("legacy.db");
    Connection::open(&catalog)
        .unwrap()
        .execute_batch(
            "CREATE TABLE catalog_models (model_id TEXT, parameter_effort TEXT);
             CREATE TABLE catalog_benchmarks
                (model_id TEXT, aa_intelligence REAL, cost_per_task_usd REAL);
             INSERT INTO catalog_models VALUES ('vendor/model','high');
             INSERT INTO catalog_benchmarks VALUES ('vendor/model',58,0);",
        )
        .unwrap();
    let attempt = json!({
        "attempt_id": "run-a", "card_key": "demo:A", "role": "review",
        "task_class": "large_regression", "leg": "a", "requested_model": "vendor/model",
        "harness": "codex", "effort": "high", "exit_class": "ok", "cost": 2.0,
        "started_at": "2026-11-22T03:00:00+00:00",
        "route": {"eligible_n": 6, "pin": false, "top5": [
            {"leg":"a","effort":"high"}, {"leg":"unknown","effort":"high"}],
            "baselines": {"static_v0":"b"}}
    });
    let attempts = tmp.path().join("attempts.json");
    let mut before = attempt.clone();
    before["attempt_id"] = json!("run-old");
    before["started_at"] = json!("2026-11-21T23:59:59Z");
    let mut without_class = attempt.clone();
    without_class["attempt_id"] = json!("run-legacy");
    without_class["task_class"] = Value::Null;
    std::fs::write(
        &attempts,
        json!([before, without_class, attempt]).to_string(),
    )
    .unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_plangraph"))
        .args(["route-shadow", "--since", "2026-11-22", "--attempts"])
        .arg(&attempts)
        .arg("--catalog")
        .arg(&catalog)
        .arg("--json")
        .output()
        .unwrap();
    assert!(output.status.success(), "{:?}", output);
    let report: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(report["bench_source"], "legacy");
    assert_eq!(report["excluded_unclassified"], 1);
    assert_eq!(report["rows"][0]["n"], 1);
    let row = &report["rows"][0];
    assert_eq!(row["role"], "review");
    assert_eq!(row["fixed"]["leg"], "b");
    assert_eq!(row["fixed"]["observed_n"], 0);
    assert_eq!(row["utility"]["leg"], "a");
    assert_eq!(row["utility_complete_n"], 0);
    assert_eq!(row["utility"]["first_pass_rate"], 1.0);
    assert_eq!(row["utility"]["verification_unknown_n"], 1);
    assert_eq!(row["utility"]["cost_per_verified_success_usd"], Value::Null);
}

#[test]
fn uses_keeper_read_endpoints_and_matches_verified_run_evidence() {
    use std::io::{BufRead, BufReader, Write};
    use std::net::TcpListener;
    use std::time::{Duration, Instant};

    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    let address = listener.local_addr().unwrap();
    let attempts = json!([{
        "attempt_id":"that-run", "card_key":"demo:K", "role":"implement",
        "task_class":"small_change", "leg":"a", "requested_model":"a", "harness":"codex",
        "started_at":"2026-11-22T00:00:00Z", "exit_class":"ok", "cost":5.0,
        "route":{"eligible_n":1,"pin":false,"top5":[{"leg":"a"}],"baselines":{"static_v0":"a"}}
    }]);
    let evidence = json!({"evidence":[
        {"kind":"run-record","verified":true,"ref":{"run_id":"other"}},
        {"kind":"run-record","verified":true,"ref":{"run_id":"that-run"}}
    ]});
    let serve = std::thread::spawn(move || {
        let deadline = Instant::now() + Duration::from_secs(5);
        for (prefix, body) in [
            ("GET /v1/attempts?since=", attempts),
            ("GET /v1/show?fmt=json&key=%64%65%6D%6F%3A%4B ", evidence),
        ] {
            let mut stream = loop {
                match listener.accept() {
                    Ok((stream, _)) => break stream,
                    Err(err)
                        if err.kind() == std::io::ErrorKind::WouldBlock
                            && Instant::now() < deadline =>
                    {
                        std::thread::sleep(Duration::from_millis(10));
                    }
                    Err(err) => panic!("missing Keeper request: {err}"),
                }
            };
            stream
                .set_read_timeout(Some(Duration::from_secs(2)))
                .unwrap();
            let lines: Vec<_> = BufReader::new(&stream)
                .lines()
                .map(Result::unwrap)
                .take_while(|line| !line.is_empty())
                .collect();
            assert!(lines[0].starts_with(prefix), "{}", lines[0]);
            let body = body.to_string();
            write!(stream,
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len()).unwrap();
        }
    });

    let tmp = tempfile::tempdir().unwrap();
    let token = tmp.path().join("token");
    std::fs::write(&token, "fixture-token").unwrap();
    let catalog = tmp.path().join("catalog.db");
    Connection::open(&catalog)
        .unwrap()
        .execute_batch(
            "CREATE VIEW catalog_benchmark_rows AS SELECT 'a' model_id, '' effort,
         'codex' harness, 50.0 score, 1.0 cost_per_task_usd, 0 legacy;",
        )
        .unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_plangraph"))
        .env("PLANGRAPH_ENV_FILE", tmp.path().join("env"))
        .env("PG_URL", format!("http://{address}"))
        .env("PG_TOKEN_FILE", token)
        .args([
            "route-shadow",
            "--since",
            "2026-11-22",
            "--json",
            "--catalog",
        ])
        .arg(catalog)
        .output()
        .unwrap();
    assert!(output.status.success(), "{:?}", output);
    serve.join().unwrap();
    let report: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(report["rows"][0]["fixed"]["verified_n"], 1);
    assert_eq!(
        report["rows"][0]["utility"]["cost_per_verified_success_usd"],
        5.0
    );
}
