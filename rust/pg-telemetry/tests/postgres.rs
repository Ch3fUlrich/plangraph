#![cfg(feature = "postgres-tests")]

use postgres::{Client, NoTls};
use serde_json::json;
use std::io::Write;
use std::process::{Command, Stdio};

fn keeper_ready(fixture: &mut Client) -> serde_json::Value {
    let input: String = fixture.query_one(
        "SELECT json_agg(row_to_json(i))::text FROM
           (SELECT items.*, ARRAY(SELECT dst FROM baseline_fixture.edges WHERE src=items.key) AS depends_on
            FROM baseline_fixture.items) i", &[]).unwrap().get(0);
    let mut python = Command::new("python3")
        .env("PYTHONPATH", concat!(env!("CARGO_MANIFEST_DIR"), "/../../src"))
        .args(["-c", "import json,sys; from plangraph.views import ready; items=json.load(sys.stdin); print(json.dumps([[i['key'] for i in ready(items,agent=agent)] for agent in (False,True)]))"])
        .stdin(Stdio::piped()).stdout(Stdio::piped()).spawn().unwrap();
    python
        .stdin
        .take()
        .unwrap()
        .write_all(input.as_bytes())
        .unwrap();
    let output = python.wait_with_output().unwrap();
    assert!(output.status.success());
    serde_json::from_slice(&output.stdout).unwrap()
}

#[test]
fn keeper_queries_share_the_full_graph_and_report_unknown_idle() {
    let dsn = std::env::var("PG_TELEMETRY_TEST_DSN").expect("PG_TELEMETRY_TEST_DSN is required");
    let mut fixture = Client::connect(&dsn, NoTls).unwrap();
    fixture
        .batch_execute(
            "CREATE SCHEMA baseline_fixture;
             CREATE ROLE baseline_fixture_reader;
             CREATE TABLE baseline_fixture.items
               (key text, project text, state text, kind text, operator bigint, priority bigint, estimate_h float8);
             CREATE TABLE baseline_fixture.edges (src text, dst text);
             CREATE TABLE baseline_fixture.events (key text, ts text, type text, seq bigint);
             INSERT INTO baseline_fixture.items VALUES
               ('a:HIGH','a','open','lane',0,9,3), ('a:TIE','a','open','lane',0,9,1),
               ('a:MANUAL','a','open','operator_task',0,10,0), ('b:LOW','b','open','lane',0,2,1),
               ('a:MIDDLE','a','open','lane',0,8,1), ('a:TAIL','a','open','lane',0,3,1),
               ('a:DEAD','a','cancelled','lane',0,10,1), ('b:BLOCKED','b','open','lane',0,8,1),
               ('a:OLD','a','done','lane',0,1,1), ('a:CYCLE1','a','open','lane',0,1,1),
               ('b:CYCLE2','b','open','lane',0,1,1);
             INSERT INTO baseline_fixture.edges VALUES
               ('a:MIDDLE','b:LOW'), ('a:TAIL','a:MIDDLE'), ('a:TAIL','b:LOW'),
               ('a:HIGH','a:OLD'), ('b:BLOCKED','a:DEAD'),
               ('a:CYCLE1','b:CYCLE2'), ('b:CYCLE2','a:CYCLE1');
             INSERT INTO baseline_fixture.events VALUES
               ('b:LOW','2020-01-01T00:00:00Z','created',1),
               ('b:LOW','2025-01-01T00:00:00Z','heartbeat',2),
               ('a:MIDDLE','2025-01-01T00:00:00Z','heartbeat',3);
             GRANT USAGE ON SCHEMA baseline_fixture TO baseline_fixture_reader;
             GRANT SELECT ON ALL TABLES IN SCHEMA baseline_fixture TO baseline_fixture_reader;",
        )
        .unwrap();

    let mut reader = dsn.parse::<postgres::Config>().unwrap();
    reader.options("-c role=baseline_fixture_reader");
    let mut reader = reader.connect(NoTls).unwrap();
    let report = pg_telemetry::report_client(&mut reader, "baseline_fixture", None);
    let recent = pg_telemetry::report_client(
        &mut reader,
        "baseline_fixture",
        Some("2024-01-01T00:00:00Z"),
    );
    let parity = keeper_ready(&mut fixture);
    fixture
        .batch_execute("DROP SCHEMA baseline_fixture CASCADE; DROP ROLE baseline_fixture_reader;")
        .unwrap();
    let report = report.unwrap();
    let recent = recent.unwrap();
    let flow = &report["q_keeper_flow"]["value"];
    assert_eq!(recent["q_keeper_flow"], report["q_keeper_flow"]);
    assert_eq!(recent["event_counts"]["n"], 2);
    assert_eq!(report["event_counts"]["n"], 3);
    assert_eq!(flow["ready"], parity[0]);
    assert_eq!(flow["ready_agent"], parity[1]);
    assert_eq!(
        flow["ready"],
        json!(["a:MANUAL", "a:TIE", "a:HIGH", "b:LOW"])
    );
    assert_eq!(flow["ready_agent"], json!(["a:TIE", "a:HIGH", "b:LOW"]));
    assert_eq!(flow["state_counts"]["a"]["open"], 6);
    assert_eq!(flow["state_counts"]["b"]["open"], 3);
    assert_eq!(flow["top_priority_ties"], json!(["a:TIE", "a:HIGH"]));
    assert_eq!(
        flow["lower_priority_dependencies"],
        json!([["a:MIDDLE", "b:LOW"], ["a:TAIL", "b:LOW"]])
    );
    let fanout = &report["q_critical_fanout"];
    assert_eq!(fanout["value"][0]["key"], "b:LOW");
    assert_eq!(fanout["value"][0]["reach"], 2);
    assert!(fanout["value"][0]["idle_age_s"].as_i64().unwrap() > 100_000_000);
    assert_eq!(fanout["value"][1]["key"], "a:CYCLE1");
    assert_eq!(fanout["value"][1]["reach"], 1);
    assert_eq!(fanout["unknown"], 3);
}
