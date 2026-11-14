use serde_json::json;
use std::collections::BTreeMap;
#[test]
fn projects_parser_and_session_path_cases() {
    let d = tempfile::tempdir().unwrap();
    let p = d.path().join("p.yaml");
    std::fs::write(&p,"# top comment\nprojects:\n  - id: a\n    # a comment line\n    path: /x/a   # trailing\n    tracks: ['t1', \"t2\"]\n    worktrees: '/x/a-wt'\n  - id: b\n    state: paused\n").unwrap();
    assert_eq!(
        pg_fleet::projects::load(&p),
        vec![
            json!({"id":"a","path":"/x/a","tracks":["t1","t2"],"worktrees":"/x/a-wt"}),
            json!({"id":"b","state":"paused"})
        ]
    );
    assert!(pg_fleet::projects::load(&d.path().join("missing")).is_empty());
    let projects = vec![
        json!({"id":"alpha","path":"/c/Alpha","orchestrator":"alpha-L1-main","tracks":["alpha-L2-general"],"worktrees":"/c/Alpha-lanes"}),
        json!({"id":"plangraph","path":"/c/plangraph"}),
        json!({"id":"server","path":"/c/Server"}),
        json!({"id":"remote","path":"(remote) C:\\x","orchestrator":"remote-L1"}),
    ];
    let specs = BTreeMap::from([
        (
            "build-L1".into(),
            json!({"cwd":"/c/plangraph-worktrees/build-L1/x"}),
        ),
        (
            "fixes-L2-acp".into(),
            json!({"cwd":"/c/Alpha/.claude/worktrees/lane"}),
        ),
    ]);
    for (name, want) in [
        ("alpha-L1-main", Some("alpha")),
        ("alpha-L2-general", Some("alpha")),
        ("remote-L1", Some("remote")),
        ("build-L1", Some("plangraph")),
        ("fixes-L2-acp", Some("alpha")),
        ("server-L2-dual", Some("server")),
        ("mystery-L1", None),
    ] {
        assert_eq!(
            pg_fleet::summary::project_of_session(name, &specs, &projects).as_deref(),
            want
        );
    }
    for (path, want) in [
        ("/c/Alpha-lanes/lane/f", Some("alpha")),
        ("/c/plangraph", Some("plangraph")),
        ("/c/plangraph-lanes/a", Some("plangraph")),
        ("/c/ServerX/y", None),
        ("(remote) C:\\x", None),
        ("", None),
    ] {
        assert_eq!(
            pg_fleet::projects::of_path(path, &projects).as_deref(),
            want
        );
    }
}
#[test]
fn fit_since_and_reference_helpers() {
    let fit = pg_fleet::summary::fit;
    assert_eq!(fit(&[], 50), "");
    assert_eq!(fit(&["a".into()], 50), "a");
    assert_eq!(fit(&["a".into(), "b".into()], 50), "a; b");
    let items: Vec<_> = (0..30)
        .map(|i| format!("ITEM-{i:03} 0123456 10-31T04:44Z"))
        .collect();
    for width in [40, 60, 100, 200] {
        let line = fit(&items, width);
        assert!(line.len() <= width || !line.contains(';'));
        let shown = line.split(" +").next().unwrap().matches(';').count() + 1;
        assert!(line.ends_with(&format!(" +{}", 30 - shown)));
        assert!(line.starts_with(&items[0]));
    }
    assert_eq!(
        fit(&["x".repeat(300), "y".into()], 50),
        "x".repeat(300) + " +1"
    );
    let now = pg_fleet::digest_text::to_dt("2026-10-31T08:00:00Z").unwrap();
    for bad in ["yesterday", "24", "h", "1.5h", ""] {
        assert!(pg_fleet::summary::parse_since(bad, now)
            .unwrap_err()
            .contains("bad --since"));
    }
    assert_eq!(
        pg_fleet::summary::parse_since("2h", now).unwrap(),
        now - chrono::TimeDelta::hours(2)
    );
    assert_eq!(
        pg_fleet::summary::parse_since("2d", now).unwrap(),
        now - chrono::TimeDelta::days(2)
    );
    assert_eq!(
        pg_fleet::summary_sources::norm_ref(&json!(87654321)),
        Some("8765432".into())
    );
}
#[test]
fn invalid_since_cli() {
    for bad in ["yesterday", "24", "h", "1.5h", ""] {
        let out = std::process::Command::new(env!("CARGO_BIN_EXE_work-summary"))
            .args(["--since", bad])
            .output()
            .unwrap();
        assert_eq!(out.status.code(), Some(2));
        assert!(out.stdout.is_empty());
        assert!(String::from_utf8_lossy(&out.stderr).contains("bad --since"));
    }
}
