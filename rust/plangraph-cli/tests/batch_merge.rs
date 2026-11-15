use std::process::Command;
use tempfile::TempDir;

fn git(repo: &TempDir, args: &[&str]) -> String {
    let output = Command::new("git")
        .current_dir(repo.path())
        .args(args)
        .output()
        .unwrap();
    assert!(output.status.success(), "{:?}: {:?}", args, output);
    String::from_utf8(output.stdout).unwrap().trim().to_owned()
}

#[test]
fn two_ready_prs_refuse_single_and_accept_frozen_batch() {
    let repo = TempDir::new().unwrap();
    git(&repo, &["init", "-q"]);
    git(
        &repo,
        &[
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.test",
            "commit",
            "--allow-empty",
            "-qm",
            "base",
        ],
    );
    let base = git(&repo, &["rev-parse", "HEAD"]);
    let mut heads = Vec::new();
    for number in [1, 2] {
        git(
            &repo,
            &["checkout", "-q", "-B", &format!("part-{number}"), &base],
        );
        std::fs::write(repo.path().join(format!("part-{number}")), "fixture").unwrap();
        git(&repo, &["add", "."]);
        git(
            &repo,
            &[
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.test",
                "commit",
                "-qm",
                "part",
            ],
        );
        heads.push(git(&repo, &["rev-parse", "HEAD"]));
    }
    git(&repo, &["checkout", "-q", "-B", "batch", &base]);
    for head in &heads {
        git(
            &repo,
            &[
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.test",
                "merge",
                "--no-ff",
                "-qm",
                "batch part",
                head,
            ],
        );
    }
    let batch = git(&repo, &["rev-parse", "HEAD"]);
    let ready = |number, head| {
        format!(
            r#"{{"pr":{number},"head":"{head}","review":"ACCEPT","fast_green":true,"ready_at":"2026-11-15T05:00:00Z"}}"#
        )
    };
    let mut facts = format!(
        r#"{{"pr":1,"head":"{}","go_row":"GO 1 {}","ready":[{},{}],"batch":null}}"#,
        heads[0],
        heads[0],
        ready(1, &heads[0]),
        ready(2, &heads[1])
    );
    let check = |facts: &str| {
        let path = repo.path().join("facts.json");
        std::fs::write(&path, facts).unwrap();
        Command::new(env!("CARGO_BIN_EXE_plangraph"))
            .current_dir(repo.path())
            .args(["batch-merge-check", path.to_str().unwrap()])
            .output()
            .unwrap()
    };
    let refused = check(&facts);
    assert!(!refused.status.success());
    assert!(
        String::from_utf8_lossy(&refused.stderr).contains("batch_required"),
        "{:?}",
        refused
    );
    facts = format!(
        r#"{{"pr":3,"head":"{batch}","go_row":"GO 3 {batch}","ready":[{},{}],"batch":{{"pr":3,"head":"{batch}","pushed_at":"2026-11-15T05:30:00Z","parts":[{},{}]}}}}"#,
        ready(1, &heads[0]),
        ready(2, &heads[1]),
        ready(1, &heads[0]),
        ready(2, &heads[1])
    );
    let accepted = check(&facts);
    assert!(accepted.status.success(), "{:?}", accepted);
    assert!(String::from_utf8_lossy(&accepted.stdout).contains("parts=1,2"));
    let frozen: serde_json::Value = serde_json::from_str(&facts).unwrap();
    let mut late = frozen.clone();
    let mut next: serde_json::Value = serde_json::from_str(&ready(4, &base)).unwrap();
    next["ready_at"] = "2026-11-15T05:30:01Z".into();
    late["ready"].as_array_mut().unwrap().push(next);
    assert!(check(&late.to_string()).status.success());
    for at in ["2026-11-15T05:29:59Z", "2026-11-15T05:30:00Z", "invalid"] {
        late["ready"][2]["ready_at"] = at.into();
        assert!(!check(&late.to_string()).status.success(), "{at}");
    }
    for mutation in [
        "missing_part",
        "wrong_head",
        "bad_ancestry",
        "not_ready",
        "late_part",
        "duplicate",
        "unknown_field",
    ] {
        let mut bad = frozen.clone();
        match mutation {
            "missing_part" => {
                bad["batch"]["parts"].as_array_mut().unwrap().pop();
            }
            "wrong_head" => bad["batch"]["head"] = base.clone().into(),
            "bad_ancestry" => bad["batch"]["parts"][0]["head"] = "f".repeat(40).into(),
            "not_ready" => bad["batch"]["parts"][0]["fast_green"] = false.into(),
            "late_part" => bad["batch"]["parts"][0]["ready_at"] = "2026-11-15T05:30:01Z".into(),
            "duplicate" => {
                let part = bad["ready"][0].clone();
                bad["ready"].as_array_mut().unwrap().push(part);
            }
            _ => bad["typo"] = true.into(),
        }
        assert!(!check(&bad.to_string()).status.success(), "{mutation}");
    }
}

#[test]
fn named_single_pr_exceptions_are_head_bound_and_only_one_is_counted() {
    use plangraph_cli::batch_merge::{check, Facts};
    let head = "a".repeat(40);
    let other = "b".repeat(40);
    let part = |pr, head: &str| {
        serde_json::json!({"pr": pr, "head": head,
        "review": "ACCEPT", "fast_green": true, "ready_at": "2026-11-15T05:00:00Z"})
    };
    let mut value = serde_json::json!({"pr": 1, "head": head, "go_row": "",
        "ready": [part(1, &head), part(2, &other)], "batch": null});
    let passes = |value: &serde_json::Value| {
        let facts: Facts = serde_json::from_value(value.clone()).unwrap();
        check(&facts, std::path::Path::new(".")).is_ok()
    };
    for exception in ["main-red-fix", "operator-order"] {
        value["go_row"] = format!("GO 1 {head} EXCEPTION:{exception}").into();
        assert!(passes(&value), "{exception}");
    }
    value["go_row"] = format!("GO 1 {head} EXCEPTION:only-one-ready-pr").into();
    assert!(!passes(&value));
    for field in ["review", "fast_green"] {
        let mut not_ready = value.clone();
        not_ready["ready"][1][field] = if field == "review" {
            "REQUEST_CHANGES".into()
        } else {
            false.into()
        };
        assert!(passes(&not_ready));
    }
    value["ready"].as_array_mut().unwrap().pop();
    assert!(passes(&value));
    for row in [
        format!("GO 2 {head} EXCEPTION:operator-order"),
        format!("GO 1 {other} EXCEPTION:operator-order"),
        format!("GO 1 {head} EXCEPTION:operator-order-typo"),
        format!("GO 1 {head} EXCEPTION:operator-order EXCEPTION:main-red-fix"),
        format!("GO 1 {head}\nEXCEPTION:operator-order"),
        format!("GO 1 {head}"),
    ] {
        value["go_row"] = row.clone().into();
        assert!(!passes(&value), "{row}");
    }
    value["go_row"] = format!("GO 1 {head} EXCEPTION:operator-order").into();
    value["ready"][0]["head"] = other.into();
    assert!(!passes(&value));
}
