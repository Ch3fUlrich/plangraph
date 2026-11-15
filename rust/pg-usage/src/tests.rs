use super::*;
use std::io::Write;

fn turn(id: &str) -> Value {
    json!({"type":"assistant", "timestamp":"2026-11-06T12:00:00Z",
        "message":{"id":id, "usage":{"input_tokens":20, "output_tokens":10,
            "cache_creation_input_tokens":30, "cache_read_input_tokens":100}}})
}
fn write(path: &Path, records: &[Value]) {
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    let mut file = fs::File::create(path).unwrap();
    for record in records {
        writeln!(file, "{record}").unwrap();
    }
}
#[test]
fn census_roles_forks_triggers_and_zero_merge_guard() {
    let temp = tempfile::tempdir().unwrap();
    for (i, name) in ROLES[..5].iter().enumerate() {
        write(
            &temp.path().join(format!("project/{i}.jsonl")),
            &[
                json!({"type":"agent-name", "agentName":name}),
                turn("parent"),
            ],
        );
    }
    let project = temp.path().join("project");
    write(
        &project.join("0/subagents/agent-fork.jsonl"),
        &[turn("parent"), turn("fork")],
    );
    write(
        &project.join("0/subagents/agent-fresh.jsonl"),
        &[turn("fresh")],
    );
    let r = report(temp.path(), "2026-11-06", json!([]));
    for name in ROLES {
        assert_eq!(r["roles"][name]["turns"], 1, "{name}");
        assert_eq!(r["roles"][name]["weighted_tokens"], 70.);
        assert!(r["roles"][name]["weighted_tokens_per_merged_pr"].is_null());
    }
    assert_eq!(r["inherited_turns_excluded"], 1);
    assert_eq!(r["guard"]["revert_required"], true);
    write(
        &project.join("triggers.jsonl"),
        &[
            json!({"type":"agent-name", "agentName":"router"}),
            json!({"type":"user", "message":{"content":"beat: synthetic"}}),
            turn("a"),
            turn("a"),
            json!({"type":"user", "trigger":"coordination tool loop"}),
            turn("b"),
            json!({"type":"user", "message":{"content":"msg-in: synthetic"}}),
            turn("c"),
        ],
    );
    let r = report(
        temp.path(),
        "2026-11-06T01:00:00Z",
        json!([
        {"number":1,"mergedAt":"2026-11-06T12:00:00Z"},
        {"number":1,"mergedAt":"2026-11-06T12:00:00Z"},
        {"number":2,"mergedAt":"2026-11-09T00:00:00Z"}]),
    );
    assert_eq!(r["merged_prs"], 1);
    assert_eq!(r["roles"]["router"]["turns"], 4);
    assert_eq!(r["roles"]["router"]["trigger_share"]["beat"], 0.25);
    assert_eq!(r["roles"]["router"]["weighted_tokens_per_merged_pr"], 280.);
    assert_eq!(r["guard"]["revert_required"], false);
}

#[test]
fn census_coordination_tool_results_and_malformed_usage() {
    let temp = tempfile::tempdir().unwrap();
    let start = instant("2026-11-06").unwrap();
    let end = instant("2026-11-09").unwrap();
    assert!(census(temp.path(), start, end, &[1.; 4], &json!([])).is_err());
    let path = temp.path().join("project/session.jsonl");
    let mut first = turn("a");
    first["message"]["content"] = json!([{"type":"tool_use","name":"SendMessage"}]);
    let mut invalid = turn("bad");
    invalid["message"]["usage"]["input_tokens"] = json!(-1);
    write(
        &path,
        &[
            first,
            json!({"type":"user","message":{"content":[{"type":"tool_result"}]}}),
            turn("b"),
            invalid,
        ],
    );
    let r = report(temp.path(), "2026-11-06", json!([]));
    assert_eq!(r["malformed_lines"], 1);
    assert_eq!(
        r["roles"]["unclassified"]["trigger_turns"]["coordination tool loop"],
        1
    );
}

fn report(root: &Path, start: &str, merges: Value) -> Value {
    census(
        root,
        instant(start).unwrap(),
        instant("2026-11-09").unwrap(),
        &[1., 1., 1., 0.1],
        &merges,
    )
    .unwrap()
}
