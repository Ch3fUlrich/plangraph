use serde_json::{json, Value};
use std::{
    fs,
    io::Write,
    process::{Command, Stdio},
};

#[path = "../../test_support/python.rs"]
mod test_python;

fn call(verb: &str, value: &Value) -> std::process::Output {
    let mut child = Command::new(env!("CARGO_BIN_EXE_pg-spawnd"))
        .arg(verb)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    child
        .stdin
        .take()
        .unwrap()
        .write_all(value.to_string().as_bytes())
        .unwrap();
    child.wait_with_output().unwrap()
}

#[test]
fn daily_coverage_joins_by_run_and_host_at_the_exact_boundary() {
    let day = "2026-11-22";
    let first: Vec<_> = (0..20)
        .map(|n| json!({"run_id":format!("run-{n}"),"host":"a"}))
        .collect();
    let mut keeper = first[..19].to_vec();
    keeper.push(keeper[0].clone());
    let mut input = json!({
        "day":day, "runs":first, "fleet":[first[0]], "keeper":keeper
    });
    let report = |input: &Value| {
        let output = call("attempt-coverage", input);
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        serde_json::from_slice::<Value>(&output.stdout).unwrap()
    };
    let exactly = report(&input);
    assert_eq!(exactly["below_threshold"], false);
    assert_eq!(
        exactly["hosts"]["a"],
        json!({"expected":20,"keeper":19,"matched":19})
    );
    input["fleet"]
        .as_array_mut()
        .unwrap()
        .push(json!({"run_id":"run-fleet","host":"b"}));
    input["keeper"]
        .as_array_mut()
        .unwrap()
        .push(json!({"run_id":"run-fleet","host":"a"}));
    let missing = report(&input);
    assert_eq!(missing["day"], day);
    assert_eq!(missing["below_threshold"], true);
    assert_eq!(
        missing["hosts"]["b"],
        json!({"expected":1,"keeper":0,"matched":0})
    );
    assert_eq!(
        missing["total"],
        json!({"expected":21,"keeper":20,"matched":19})
    );
}

#[test]
fn daily_inventory_includes_separate_fleet_evidence_and_uses_utc_end_time() {
    let state = tempfile::tempdir().unwrap();
    let midnight = 1_700_006_400;
    let snapshot = |id, ended, status, host| json!({"run_id":id,"started":midnight-400_000,"ended":ended,"status":status,"host":host});
    let save = |file: &str, row: Value| {
        let path = state.path().join(file);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, row.to_string()).unwrap();
    };
    save(
        "runs/run-midnight/record.json",
        snapshot("run-midnight", midnight, "failed", None),
    );
    save(
        "runs/run-before/record.json",
        snapshot("run-before", midnight - 1, "completed", None),
    );
    save(
        "runs/run-dry/record.json",
        snapshot("run-dry", midnight, "dry_run", None),
    );
    #[cfg(target_os = "linux")]
    save(
        "fleet-session-outcomes/run-fleet.json",
        snapshot(
            "run-fleet",
            midnight + 86_399,
            "cancelled",
            Some("other-host"),
        ),
    );

    let output = Command::new(env!("CARGO_BIN_EXE_pg-spawnd"))
        .args(["attempt-coverage-input", "2023-11-15"])
        .env("PLANGRAPH_SPAWN_STATE", state.path())
        .env("PLANGRAPH_HOST", "workstation")
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let result: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(
        result["runs"],
        json!([{"run_id":"run-midnight","host":"workstation"}])
    );
    #[cfg(target_os = "linux")]
    assert_eq!(
        result["fleet"],
        json!([{"run_id":"run-fleet","host":"other-host"}])
    );
}

#[test]
fn terminal_records_project_without_routed_legs() {
    for (kind, role, harness, status, code, leg, outcome) in [
        ("seat", "review", "claude", "completed", 0, "claude", "ok"),
        (
            "spawner",
            "implement",
            "codex",
            "failed",
            1,
            "codex",
            "worker_crash",
        ),
        (
            "spawner",
            "implement",
            "codex",
            "refused",
            2,
            "codex",
            "contract_defect",
        ),
        (
            "spawner",
            "implement",
            "codex",
            "stalled_no_leg",
            1,
            "codex",
            "stall",
        ),
        (
            "native-codex",
            "implement",
            "codex",
            "completed",
            0,
            "native-codex",
            "ok",
        ),
        (
            "native-codex",
            "implement",
            "codex",
            "failed",
            1,
            "native-codex",
            "worker_crash",
        ),
    ] {
        let record = json!({
            "run_id":"run-fixture", "host":"workstation", "role":role, "run_kind":kind,
            "harness":harness, "status":status, "exit_code":code, "started":1, "ended":2
        });
        let output = call("project-spawn-attempt", &record);
        assert!(output.status.success());
        let row: Value = serde_json::from_slice(&output.stdout).unwrap();
        assert_eq!(row["attempt_id"], "run-fixture");
        assert_eq!(row["role"], role);
        assert_eq!(row["host"], "workstation");
        assert_eq!(row["harness"], harness);
        assert_eq!(row["leg"], leg);
        assert_eq!(row["task_class"], "adhoc");
        assert_eq!(row["exit_class"], outcome);
    }
}

#[test]
fn null_projection_leaves_delivery_pending() {
    let state = tempfile::tempdir().unwrap();
    let output = test_python::command()
        .args([
            "-c",
            r#"
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from plangraph.spawner.notify import retry_spawn_attempts

state = Path(sys.argv[1])
run = dict(run_id='run-fixture', ended=2, status=True)
keeper = SimpleNamespace(url='fixture', token_file='fixture')
with patch('plangraph.fleet_snapshot._projection_binary', return_value=sys.argv[2]):
    assert retry_spawn_attempts(state, [run], keeper=keeper) == []
assert not (state / 'runs/run-fixture/attempt.delivered').exists()
"#,
        ])
        .arg(state.path())
        .arg(env!("CARGO_BIN_EXE_pg-spawnd"))
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
}

#[test]
fn terminal_spawn_projects_final_leg_once() {
    for (role, item) in [
        ("implement", Some("demo:A")),
        ("review", Some("demo:A")),
        ("review", None),
    ] {
        let leg = json!({"id":"winner","model":"requested","effort":"high","harness":"codex"});
        let run = json!({
            "run_id":"run-fixture","host":"fixture","role":role,"item_key":item,
            "started":50.0,"ended":60.0,"exit_code":0,"status":"completed",
            "plan":{"legs":[leg],"static_v0":"winner","effort":{"level":"L3","reason":"high_risk",
                "bucket":"S1","risk_band":"high","task_class":"small_change"},
                "candidates":[{"leg":"winner","u_core":3.0,"p_source":"prior",
                    "intelligence":50.0,"cost_per_task":0.2}],"skipped":[{"leg":"other","reason":"quota"}]},
            "attempts":[{"leg":{"id":"first"},"status":"failed","served_model":"earlier"},
                {"leg":leg,"status":"completed","served_model":"witnessed"},
                {"leg":"not-launched","status":"skipped"}]
        });
        let output = call("project-spawn-attempt", &run);
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        let value: Value = serde_json::from_slice(&output.stdout).unwrap();
        assert_eq!(value["attempt_id"], "run-fixture");
        assert_eq!(value["card_key"], json!(item));
        assert_eq!(value["host"], "fixture");
        assert_eq!(value["role"], role);
        assert_eq!(value["requested_model"], "requested");
        assert_eq!(value["served_model"], "witnessed");
        assert_eq!(value["level"], "L3");
        assert_eq!(value["level_reason"], "high_risk");
        assert_eq!(value["bucket"], "S1");
        assert_eq!(value["task_class"], "small_change");
        assert_eq!(value["risk_band"], "high");
        assert_eq!(
            value["route"]["baselines"],
            json!({"static_v0":"winner","strongest":"winner","cheapest":"winner"})
        );
        assert!(call("validate-attempt-route", &value["route"])
            .status
            .success());
        let mut invalid = value["route"].clone();
        invalid["top5"][0]["n"] = json!(-1);
        assert!(!call("validate-attempt-route", &invalid).status.success());
    }

    let leg = json!({"id":"manual","model":"requested","effort":"high","harness":"codex"});
    let pinned = json!({
        "run_id":"run-pin","host":"fixture","role":"implement","started":50.0,"ended":60.0,
        "status":"completed","exit_code":0,"plan":{"legs":[leg],"p_source":"override"},
        "attempts":[{"leg":leg,"status":"completed","served_model":"witnessed"}]
    });
    let output = call("project-spawn-attempt", &pinned);
    assert!(output.status.success());
    let row: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(row["route"]["pin"], true);
}
