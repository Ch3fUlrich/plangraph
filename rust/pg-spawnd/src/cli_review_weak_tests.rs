use super::tests::git_ok;
use super::*;
use pg_worker_core::run::record::new_run_id;

#[test]
fn background_only_288_byte_review_is_no_verdict() {
    let dir = tempfile::tempdir().unwrap();
    git_ok(dir.path(), &["init", "-q"]);
    git_ok(
        dir.path(),
        &[
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.test",
            "commit",
            "--allow-empty",
            "-qm",
            "fixture",
        ],
    );
    let head = git(dir.path(), &["rev-parse", "HEAD"]).ok().unwrap();
    let leg = json!({"harness":"agy", "model":"gemini-4-neon-medium", "family":"gemini"});
    let prefix =
        "I am running tests in the background. I will inspect the results when they finish.\n";
    let tail = "\nFINDING none\nVERDICT: PASS\nTEST_GAMING: no\n";
    let answer = format!(
        "{prefix}{}{tail}",
        " ".repeat(288 - prefix.len() - tail.len())
    );
    assert_eq!(answer.len(), 288);
    let mut record = crate::runs::create(
        dir.path(),
        &new_run_id(),
        "fixture",
        json!({"role":"review", "head_sha":head, "cwd":dir.path(), "exit_code":0,
            "attempts":[{"leg":leg,"status":"running"}]}),
        Status::Completed,
    )
    .ok()
    .unwrap();
    let log = record.path().with_file_name("live.log");
    std::fs::write(
        &log,
        format!(
            "{}\n",
            json!({"event":"result", "result":{"status":"SUCCESS", "response":answer}})
        ),
    )
    .unwrap();
    record.set("live_log", json!(log));
    crate::runs::finish(&mut record, Status::Completed, &leg, 1);
    let value = record.to_value();
    assert_eq!(value["verdict"], "no_verdict");
    assert_eq!(value["review_status"], "no_verdict");
    assert_eq!(value["reason"], "weak_verdict");
    assert_ne!(value["attempts"][0]["verdict"], "pass");
}

#[test]
fn review_paths_cover_requested_diff_and_root_commit() {
    let dir = tempfile::tempdir().unwrap();
    git_ok(dir.path(), &["init", "-q"]);
    for name in ["first.txt", "second.txt"] {
        std::fs::write(dir.path().join(name), "reviewed\n").unwrap();
        git_ok(dir.path(), &["add", name]);
        git_ok(
            dir.path(),
            &[
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.test",
                "commit",
                "-qm",
                name,
            ],
        );
    }
    let paths = crate::review_citation::diff_paths(dir.path(), Some("HEAD~1"), "HEAD");
    assert_eq!(paths, vec!["second.txt"]);
    assert!(crate::review_citation::names_file(
        "Reviewed second.txt",
        &paths
    ));
    assert!(!crate::review_citation::names_file(
        "Reviewed first.txt",
        &paths
    ));
    assert_eq!(
        crate::review_citation::diff_paths(dir.path(), None, "HEAD~1"),
        vec!["first.txt"]
    );
    assert!(
        crate::review_citation::diff_paths(dir.path(), Some("missing-base"), "HEAD").is_empty()
    );
}

#[test]
fn an_inline_seat_records_its_merge_base_scope_and_path_subset_for_the_classifier() {
    let dir = tempfile::tempdir().unwrap();
    let repo = dir.path();
    git_ok(repo, &["init", "-q", "-b", "main"]);
    let commit = |name: &str, text: &str| {
        std::fs::create_dir_all(repo.join(name).parent().unwrap()).unwrap();
        std::fs::write(repo.join(name), text).unwrap();
        git_ok(repo, &["add", "."]);
        git_ok(
            repo,
            &[
                "-c",
                "user.name=F",
                "-c",
                "user.email=f@example.test",
                "commit",
                "-qm",
                name,
            ],
        );
    };
    commit("base.txt", "base");
    let base = git(repo, &["rev-parse", "HEAD"]).ok().unwrap();
    commit("src/a.py", "a");
    commit("docs/b.md", "b");
    let head = git(repo, &["rev-parse", "HEAD"]).ok().unwrap();
    let reference = json!({"base": base, "paths": ["src/a.py"]});
    let dispatch = Dispatch::new(repo, head.clone(), &reference).ok().unwrap();
    assert!(
        dispatch.binding.is_none(),
        "a path-limited diff has no full-diff binding"
    );
    let mut metadata = json!({});
    dispatch.apply(&mut metadata);
    let scope = metadata["review_scope"].clone();
    assert_eq!(scope["paths"], json!(["src/a.py"]));
    // The Python classifier re-verifies the same record: only the shown path is in scope.
    let out = crate::test_python::command()
        .args([
            "-c",
            "import json, sys; from plangraph.review.verdict import bound_scope_paths; s = json.loads(sys.argv[2]); print(json.dumps(sorted(bound_scope_paths(s, sys.argv[1]) or [])))",
        ])
        .arg(repo)
        .arg(scope.to_string())
        .env("PYTHONPATH", Path::new(env!("CARGO_MANIFEST_DIR")).join("../../src"))
        .output()
        .unwrap();
    assert_eq!(
        String::from_utf8_lossy(&out.stdout).trim(),
        r#"["src/a.py"]"#,
        "{out:?}"
    );
    // A full-diff dispatch records a binding and no scope.
    let full = Dispatch::new(repo, head, &json!({"base": base}))
        .ok()
        .unwrap();
    assert!(full.binding.is_some() && full.scope.is_none());
}
