//! The completed-head gate of the two harnesses: the same scrub-hit commit is rejected by the
//! native Codex host (`Host::checks_with`) and by the Claude completion (`recover_with`), and
//! each keeps the same gate diagnostic.

use super::*;
use serde_json::json;
use std::{
    fs,
    path::{Path, PathBuf},
    process::Command,
};

fn git(dir: &Path, args: &[&str]) -> String {
    let out = Command::new("git")
        .arg("-C")
        .arg(dir)
        .args(["-c", "user.name=t", "-c", "user.email=t@example.test"])
        .args(args)
        .output()
        .unwrap();
    assert!(out.status.success(), "{args:?}: {out:?}");
    String::from_utf8_lossy(&out.stdout).trim().to_owned()
}

/// A repository whose scrub script fails on a words file that holds `private`.
fn scrub_repo(root: &Path) -> (PathBuf, PathBuf, String) {
    let repo = root.join("worktree");
    fs::create_dir_all(repo.join("tools")).unwrap();
    git(&repo, &["init", "-q", "-b", "main"]);
    fs::write(
        repo.join("tools/preflight_private_scrub.py"),
        "import sys\nsys.exit('scrub hit: fixture' if 'private' in open(sys.argv[sys.argv.index('--words') + 1]).read() else 0)\n",
    )
    .unwrap();
    git(&repo, &["add", "."]);
    git(&repo, &["commit", "-q", "-m", "base"]);
    let base = git(&repo, &["rev-parse", "HEAD"]);
    let words = root.join("words.txt");
    fs::write(&words, "private\n").unwrap();
    git(
        &repo,
        &[
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "work\n\nPlan-Item: demo:ASSIGNED",
        ],
    );
    (repo, words, base)
}

#[test]
fn both_harnesses_reject_the_same_scrub_hit_head_and_keep_the_same_diagnostic() {
    let root = tempfile::tempdir().unwrap();
    let (repo, words, base) = scrub_repo(root.path());
    let head = git(&repo, &["rev-parse", "HEAD"]);

    // Native Codex host.
    let host = Host {
        repo: repo.clone(),
        run_id: "run-gate-fixture".into(),
        cwd: repo.clone(),
        reviews: root.path().join("reviews"),
        record_path: root.path().join("record.json"),
        base_ref: "refs/heads/main".into(),
        tool_path: std::env::var("PATH").unwrap(),
        effort: WRITER_EFFORT.into(),
        state: root.path().to_owned(),
        brief: "Item key: demo:ASSIGNED\n".into(),
        push_ref: None,
        reviews_started: 0,
        review_override: None,
        review_cli: vec!["false".into()],
        python_src: None,
    };
    git(&repo, &["update-ref", &host.base_pin(), &base]);
    let codex = host
        .checks_with(Some(words.clone()))
        .expect_err("scrub hit rejected");
    assert!(codex.contains("scrub hit: fixture"), "{codex}");

    // Claude completion: the lifecycle recovery of a finished fleet session.
    let state = root.path().join("claude-state");
    let run = pg_spawnd::external_run::Registered::start(
        state.clone(),
        json!({"run_kind":"fleet-session","fleet_session_name":"writer-0001","role":"implement",
               "host":"fixture","session_id":"sid-1","item_keys":["demo:ASSIGNED"],
               "admitted_base":base,"cwd":repo}),
    )
    .unwrap();
    let id = run.id().to_owned();
    let roster = || {
        Ok(vec![
            json!({"kind":"background","sessionId":"sid-1","state":"done"}),
        ])
    };
    pg_spawnd::external_run::recover_with(&state, &roster, Some(words)).unwrap();
    let record: Value = serde_json::from_slice(
        &fs::read(state.join("runs").join(&id).join("record.json")).unwrap(),
    )
    .unwrap();
    assert_eq!(record["status"], "failed", "{record}");
    assert_eq!(record["reason"], "head_gate_rejected");
    assert_eq!(record["head_gate"]["head"], json!(head));
    // The witness keeps the first observation, gate verdict included.
    let witness: Value = serde_json::from_slice(
        &fs::read(
            state
                .join(pg_spawnd::external_run::FLEET_OUTCOMES)
                .join(format!("{id}.json")),
        )
        .unwrap(),
    )
    .unwrap();
    assert_eq!(witness["head_gate"]["ok"], false);
    // Both harnesses report the one gate output.
    assert_eq!(record["head_gate"]["diagnostic"], json!(codex));
}
