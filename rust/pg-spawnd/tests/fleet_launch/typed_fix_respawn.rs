use super::{Launcher, Sandbox};
use serde_json::json;
use std::{fs, path::Path};

pub(super) fn check(launcher: &Launcher) {
    fn git(cwd: &Path, args: &[&str]) -> String {
        let output = pg_worker_core::git_test_support::command(cwd)
            .args(args)
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{args:?}: {}",
            String::from_utf8_lossy(&output.stderr)
        );
        String::from_utf8(output.stdout).unwrap().trim().to_owned()
    }

    let s = Sandbox::new(launcher);
    let work = s.path("work");
    git(&work, &["init", "-b", "main"]);
    git(&work, &["commit", "--allow-empty", "-m", "pushed fix base"]);
    let pushed = git(&work, &["rev-parse", "HEAD"]);
    let origin = s.path("origin.git");
    git(&work, &["clone", "--bare", ".", origin.to_str().unwrap()]);
    git(
        &work,
        &["remote", "add", "origin", origin.to_str().unwrap()],
    );
    fs::create_dir(work.join("status")).unwrap();
    fs::write(work.join("status/writer.md"), "# Fix-round handoff\n").unwrap();
    fs::write(
        work.join("status/writer.card.md"),
        "# writer card · writer-0001 · 2026-11-22T04:00Z\n",
    )
    .unwrap();
    git(&work, &["add", "status"]);
    git(&work, &["commit", "-m", "local fix and handoff"]);
    let local = git(&work, &["rev-parse", "HEAD"]);

    let mut policy = s.json("router/ops/fleet-launch-policy.json");
    policy["writer_brief"] = json!({"revision":"fixture", "rules":[]});
    s.write_json("router/ops/fleet-launch-policy.json", policy.clone());
    let input = json!({
        "items": [{"key":"demo:FIXTURE", "title":"Fixture",
            "description":"Why: verify restart\nHow: reuse the existing checkout",
            "done_criteria":"Resume the local fix."}],
        "run": {"name":"fixture", "base":"origin/main", "status_file":"status.txt",
            "review":{"owner":"writer", "command":"fixture-review", "wait_seconds":60},
            "policy_revision":"fixture", "rules":[],
            "trailers":["Plan-Item: demo:FIXTURE", "Writer: fixture"]},
        "policy": policy["writer_brief"],
        "pushed_fix": {"reference":"refs/heads/main", "sha":pushed},
    });
    fs::write(
        s.path("prompt"),
        format!("{}\n{input}", pg_worker_core::writer_brief::PREFIX),
    )
    .unwrap();

    let fresh = s.launch("writer").output().unwrap();
    assert_eq!(
        fresh.status.code(),
        Some(2),
        "{}",
        s.normalize(&fresh.stderr)
    );
    assert!(s
        .normalize(&fresh.stderr)
        .contains("local HEAD contains commits"));
    assert!(!s.path("child.json").exists());

    let resumed = s.launch("writer").arg("--respawn").output().unwrap();
    assert_eq!(
        resumed.status.code(),
        Some(0),
        "{}",
        s.normalize(&resumed.stderr)
    );
    let child = s.json("child.json");
    let prompt = child["argv"]
        .as_array()
        .unwrap()
        .last()
        .unwrap()
        .as_str()
        .unwrap();
    assert!(
        prompt.contains("Resume the local fix.") && prompt.contains("Run `resume-digest` first.")
    );
    assert_eq!(git(&work, &["rev-parse", "HEAD"]), local);
}
