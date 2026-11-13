use serde_json::Value;
#[test]
fn original_go_gate_scenarios() {
    for line in include_str!("fixtures/go.jsonl").lines() {
        let v: Value = serde_json::from_str(line).unwrap();
        let result = pg_fleet::go_gate::evaluate(
            v["text"].as_str().unwrap(),
            v["go"].as_str().unwrap(),
            v["head"].as_str().unwrap(),
            v["key"].as_str(),
            v["session"].as_str(),
            |_| match v["keeper"].as_str().unwrap() {
                "unavailable" => Err("keeper wrapper unavailable".into()),
                "missing" => Ok(1),
                _ => Ok(0),
            },
        );
        assert_eq!(
            result,
            (
                v["code"].as_i64().unwrap() as i32,
                v["stdout"].as_str().unwrap().to_owned()
            ),
            "{}",
            v["name"]
        );
    }
}

#[test]
fn keeper_failures_are_private_and_session_checked_after_keeper() {
    let head = "a".repeat(40);
    let text=format!("| D-1 | p | GO-HEAD {head}; PLAN p:ITEM conforms=yes deviations=none | merge only by owner |\n");
    for reason in ["keeper wrapper unavailable", "keeper timed out p:ITEM"] {
        assert_eq!(
            pg_fleet::go_gate::evaluate(&text, "D-1", &head, Some("p:ITEM"), None, |_| Err(
                reason.into()
            )),
            (5, format!("STOP: {reason}\n"))
        );
    }
    assert_eq!(
        pg_fleet::go_gate::evaluate(&text, "D-1", &head, Some("p:ITEM"), None, |_| Ok(1)),
        (5, "STOP: no keeper item p:ITEM\n".into())
    );
    assert_eq!(
        pg_fleet::go_gate::evaluate(&text, "D-1", &head, Some("p:ITEM"), None, |_| Ok(0)),
        (5, "STOP: merge only by owner\n".into())
    );
}

#[test]
fn cli_missing_wrapper_and_short_head() {
    let d = tempfile::tempdir().unwrap();
    let log = d.path().join("log.md");
    let head = "a".repeat(40);
    std::fs::write(
        &log,
        format!("| D-1 | p | GO-HEAD {head}; PLAN p:ITEM conforms=yes deviations=none |\n"),
    )
    .unwrap();
    let run = |head: &str| {
        std::process::Command::new(env!("CARGO_BIN_EXE_go-row-gate"))
            .args([
                "--go",
                "D-1",
                "--head",
                head,
                "--key",
                "p:ITEM",
                "--log",
                log.to_str().unwrap(),
            ])
            .env("HOME", d.path())
            .env("PATH", d.path())
            .env_remove("PG_PGK")
            .output()
            .unwrap()
    };
    let out = run(&head);
    assert_eq!(out.status.code(), Some(5));
    assert_eq!(out.stdout, b"STOP: keeper wrapper unavailable\n");
    assert!(out.stderr.is_empty());
    assert_eq!(run("aaaaaaaa").status.code(), Some(2));
}
