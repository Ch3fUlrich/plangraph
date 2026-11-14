use pg_ctx::{
    keeper_reconcile::{apply_batch, propose, Assessment},
    keeper_truth::{parse_commits, RepositoryIndex},
};
use serde_json::json;
use std::collections::BTreeMap;

#[test]
fn keeper_reconcile_open_review_and_slice() {
    let mut index = RepositoryIndex::default();
    index.add_merged(
        parse_commits(&format!(
            "{}\x1f2026-11-13\x1fwork (#42)\x1fdemo:A\x1eB\x1eC\x1d",
            "a".repeat(40)
        ))
        .unwrap(),
    );
    let items = vec![
        json!({"key":"demo:A","state":"open","done_criteria":"done"}),
        json!({"key":"demo:B","state":"in_review","done_criteria":"done"}),
        json!({"key":"demo:C","state":"claimed","done_criteria":"done"}),
        json!({"key":"demo:D","state":"done"}),
    ];
    let assessment: Assessment = serde_json::from_value(json!({"all_slices_merged":false,
        "done_criteria":"done", "commits":["a".repeat(40)],"prs":[42],"families":["openai"],"reason":"slice two remains"})).unwrap();
    let rows = propose(
        &items,
        &index,
        &BTreeMap::from([("demo:C".into(), assessment)]),
    )
    .unwrap();
    assert_eq!(rows.len(), 3);
    assert_eq!(rows[0].state, "open");
    assert_eq!(rows[1].state, "in_review");
    assert_eq!(rows[2].action, "SLICE-OPEN");
    assert_eq!(rows[2].commit_count, 1);
    assert_eq!(rows[2].prs, vec![42]);
    assert!(rows[2].note.contains("slice two remains"));
}

#[test]
fn keeper_reconcile_batch_reuses_one_router_decision() {
    let rows: Vec<pg_ctx::keeper_reconcile::Row> = serde_json::from_value(json!([
        {"key":"demo:A","state":"in_review","action":"CLOSE","prs":[42],"commit_count":1,"last_commit_date":"today","commits":["a".repeat(40)],"families":["openai"],"note":"DC covered"},
        {"key":"demo:B","state":"in_review","action":"CLOSE","prs":[43],"commit_count":1,"last_commit_date":"today","commits":["b".repeat(40)],"families":["google"],"note":"DC covered"}
    ])).unwrap();
    let mut sent = Vec::new();
    apply_batch(&rows, "D-6000", |key, decision, families| {
        sent.push((key.to_owned(), decision.to_owned(), families.to_vec()));
        Ok(())
    })
    .unwrap();
    assert_eq!(sent.len(), 2);
    assert!(sent.iter().all(|row| row.1 == "D-6000"));
    assert!(apply_batch(&rows, "made-up", |_, _, _| panic!("must validate first")).is_err());
}

#[test]
fn keeper_reconcile_alert_once_after_day_and_reset() {
    use pg_ctx::keeper_reconcile::AlertState;
    let mut state = AlertState::default();
    assert!(!state.tick(3, 100));
    assert!(!state.tick(4, 86500));
    assert!(state.tick(1, 86501));
    let mut state: AlertState =
        serde_json::from_str(&serde_json::to_string(&state).unwrap()).unwrap();
    assert!(!state.tick(20, 200000));
    assert!(!state.tick(0, 200001));
    assert!(!state.tick(1, 200002));
    assert!(state.tick(1, 286403));
}

#[test]
fn keeper_reconcile_unknown_never_closes_and_output_is_tsv() {
    let mut index = RepositoryIndex::default();
    index.add_merged(
        parse_commits(&format!(
            "{}\x1f2026-11-13\x1fwork\x1fdemo:A\x1d",
            "a".repeat(40)
        ))
        .unwrap(),
    );
    let items = vec![json!({"key":"demo:A","state":"open","done_criteria":"new scope"})];
    let stale: Assessment = serde_json::from_value(json!({"all_slices_merged":true,
        "done_criteria":"old scope", "commits":["a".repeat(40)],"prs":[42],"families":["openai"],"reason":"covered"})).unwrap();
    let rows = propose(&items, &index, &BTreeMap::from([("demo:A".into(), stale)])).unwrap();
    assert_eq!(rows[0].action, "CHECK");
    apply_batch(&rows, "D-6000", |_, _, _| panic!("CHECK cannot close")).unwrap();
    let output = pg_ctx::keeper_reconcile::tsv(&rows);
    assert_eq!(output.lines().count(), 2);
    assert_eq!(output.lines().nth(1).unwrap().split('\t').count(), 7);
}

#[test]
fn keeper_reconcile_reads_real_git_trailers_and_introducing_merge() {
    use pg_ctx::{keeper_truth::history, truth_process::run};
    let repo = tempfile::tempdir().unwrap();
    let git = |args: &[&str]| {
        let mut argv = vec![
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "commit.gpgSign=false",
            "-c",
            "core.hooksPath=/dev/null",
        ];
        argv.extend_from_slice(args);
        run(repo.path(), "git", &argv).unwrap()
    };
    git(&["init", "-q", "-b", "main"]);
    git(&["commit", "-q", "--allow-empty", "-m", "base"]);
    git(&["checkout", "-q", "-b", "work"]);
    git(&[
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "work\n\nPlan-Item: demo:A\nPlan-Item: demo:B",
    ]);
    git(&["checkout", "-q", "main"]);
    git(&["merge", "-q", "--no-ff", "work", "-m", "Merge PR #42"]);
    let commits = history(repo.path(), "main").unwrap();
    let mut index = RepositoryIndex::default();
    index.add_merged(commits);
    assert_eq!(index.work("demo:A").commits.len(), 1);
    assert_eq!(index.work("demo:A").commits[0].prs, vec![42]);
    assert_eq!(index.work("demo:B").commits.len(), 1);
    assert!(index.work("demo:C").commits.is_empty());
}
