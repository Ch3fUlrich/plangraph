#![cfg(target_os = "linux")]
//! `live_run` counts only unfinished runs; a pin or a run in an ancestor directory is not one.
use pg_reap::parse_porcelain;
use pg_worker_core::{
    disk_hygiene::Guards,
    sweep_facts::{facts, Claims, Protection},
};
use std::{
    fs,
    path::Path,
    time::{Duration, SystemTime},
};

fn git(repo: &Path, args: &[&str]) -> String {
    let out = pg_worker_core::git_test_support::command(repo)
        .args([
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
        ])
        .args(args)
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{args:?}: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    String::from_utf8(out.stdout).unwrap()
}

fn protection(guards: Guards) -> Protection {
    Protection {
        guards,
        claims: Claims {
            paths: vec![],
            branches: vec![],
        },
        units: vec![],
        unit_refs: vec![],
        cwds: vec![],
    }
}

fn record(state: &Path, id: &str, status: &str, cwd: &Path) {
    let dir = state.join("runs").join(id);
    fs::create_dir_all(&dir).unwrap();
    fs::write(
        dir.join("record.json"),
        serde_json::json!({"status": status, "cwd": cwd}).to_string(),
    )
    .unwrap();
}

#[test]
fn finished_runs_pins_and_ancestor_runs_are_not_live_run() {
    let tmp = tempfile::tempdir().unwrap();
    let repo = tmp.path().join("repo");
    fs::create_dir(&repo).unwrap();
    git(&repo, &["init", "-qb", "main"]);
    fs::write(repo.join("one"), "one").unwrap();
    git(&repo, &["add", "."]);
    git(&repo, &["commit", "-qm", "initial"]);
    git(&repo, &["update-ref", "refs/remotes/origin/main", "HEAD"]);
    let tree = repo.join(".claude/worktrees/lane");
    git(
        &repo,
        &["worktree", "add", "-qb", "lane", tree.to_str().unwrap()],
    );
    let trees = parse_porcelain(&git(&repo, &["worktree", "list", "--porcelain"])).unwrap();
    let current = trees.iter().find(|w| w.path.ends_with("lane")).unwrap();
    let now = SystemTime::now() + Duration::from_secs(90000);
    let flags = |guards: Guards| {
        let f = facts(&repo, current, &protection(guards), &[], now).unwrap();
        (f.live_run, f.pinned)
    };
    let state = tmp.path().join("state");
    let pins = tmp.path().join("pins.json");
    fs::write(&pins, "[]").unwrap();

    // A finished run on the tree is not live; an unfinished run on it is.
    record(&state, "run-done", "completed", &tree);
    assert_eq!(flags(Guards::load(&pins, &state).unwrap()), (false, false));
    record(&state, "run-live", "running", &tree.join("rust"));
    assert_eq!(flags(Guards::load(&pins, &state).unwrap()), (true, false));

    // An unfinished run in the repository root does not own the trees below it.
    let guards = Guards {
        pinned: vec![],
        live: vec![repo.clone()],
    };
    assert_eq!(flags(guards), (false, false));

    // A pin protects the tree under its own reason and is not counted as a run.
    let guards = Guards {
        pinned: vec![tree.clone()],
        live: vec![],
    };
    assert_eq!(flags(guards), (false, true));
}
