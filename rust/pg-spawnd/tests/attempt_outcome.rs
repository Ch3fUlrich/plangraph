use serde_json::{json, Value};
use std::{
    io::Write,
    process::{Command, Stdio},
};

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
