use pg_ctx::keeper_truth::{parse_commits, RepositoryIndex};

#[test]
fn keeper_truth_exact_trailers_and_open_prs() {
    let log = format!(
        "{}\x1f2026-11-13\x1fwork\x1falpha:A\x1eA-FU\x1d",
        "a".repeat(40)
    );
    let commits = parse_commits(&log).unwrap();
    let mut index = RepositoryIndex::default();
    index.add_merged(commits.clone());
    index.add_pr(42, commits);
    assert_eq!(index.work("alpha:A").commits.len(), 1);
    assert_eq!(index.work("alpha:A").prs, vec![42]);
    assert!(index.work("alpha:AB").commits.is_empty());
    assert_eq!(index.work("alpha:A-FU").commits.len(), 1);
}

#[test]
fn keeper_truth_malformed_history_is_unknown() {
    assert!(parse_commits("truncated output").is_err());
}
