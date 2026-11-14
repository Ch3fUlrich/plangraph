#![cfg(target_os = "linux")]
use std::{fs, io::Write, path::Path, process::Command};
fn command(root: &Path, name: &str) -> Command {
    let mut c = Command::new(name);
    c.env_clear()
        .env("PATH", std::env::var_os("PATH").unwrap())
        .env("HOME", root)
        .env("XDG_CONFIG_HOME", root)
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_CONFIG_SYSTEM", "/dev/null")
        .env("GIT_AUTHOR_NAME", "Fixture")
        .env("GIT_AUTHOR_EMAIL", "fixture@example.invalid")
        .env("GIT_COMMITTER_NAME", "Fixture")
        .env("GIT_COMMITTER_EMAIL", "fixture@example.invalid");
    c
}
fn git(root: &Path, repo: &Path, args: &[&str]) -> String {
    let out = command(root, "git")
        .arg("-C")
        .arg(repo)
        .args(args)
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    String::from_utf8(out.stdout).unwrap().trim().into()
}
#[test]
fn executes_outcomes_and_backs_up_dirty_attempts() {
    for (outcome, age, protected, dirty, removed) in [
        ("merged", 0, false, false, true),
        ("merged", 0, false, true, true),
        ("failed", 14 * 86400, false, true, true),
        ("cancelled", 14 * 86400, false, false, true),
        ("failed", 14 * 86400 - 1, false, false, true),
        ("cancelled", 14 * 86400, true, false, true),
        ("succeeded", 0, false, false, true),
        ("running", 0, false, false, false),
    ] {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path();
        let repo = root.join("repo");
        let remote = root.join("origin.git");
        fs::create_dir(&repo).unwrap();
        fs::create_dir(&remote).unwrap();
        git(root, &remote, &["init", "--bare", "--initial-branch=main"]);
        git(root, &repo, &["init", "--initial-branch=main"]);
        fs::write(repo.join("file"), "base").unwrap();
        git(root, &repo, &["add", "file"]);
        git(root, &repo, &["commit", "-m", "fixture"]);
        git(
            root,
            &repo,
            &["remote", "add", "origin", remote.to_str().unwrap()],
        );
        git(root, &repo, &["push", "origin", "main"]);
        let host = root.join("attempts");
        fs::create_dir(&host).unwrap();
        let path = host.join("attempt-test");
        git(
            root,
            &repo,
            &["worktree", "add", "-b", "lane/test", path.to_str().unwrap()],
        );
        if dirty {
            fs::write(path.join("file"), "modified").unwrap();
            fs::write(path.join("untracked"), "precious").unwrap();
        }
        let request = serde_json::json!({"repo":repo,"root":host,"path":path,"branch":"lane/test","outcome":outcome,"age_seconds":age,"open_pr":protected,"live_claim":false});
        let mut child = command(root, env!("CARGO_BIN_EXE_pg-reap"))
            .arg("finish-attempt")
            .stdin(std::process::Stdio::piped())
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped())
            .spawn()
            .unwrap();
        child
            .stdin
            .take()
            .unwrap()
            .write_all(&serde_json::to_vec(&request).unwrap())
            .unwrap();
        let output = child.wait_with_output().unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        assert_eq!(!path.exists(), removed);
        if dirty && removed {
            let backup = host.join("backups/attempt-test");
            assert!(fs::read_to_string(backup.join("tracked.patch"))
                .unwrap()
                .contains("modified"));
            let list = command(root, "tar")
                .arg("-tf")
                .arg(backup.join("worktree.tar"))
                .output()
                .unwrap();
            assert!(String::from_utf8_lossy(&list.stdout).contains("untracked"));
        }
        if outcome == "failed" && age < 14 * 86400 {
            assert!(!git(root, &repo, &["rev-parse", "refs/heads/lane/test"]).is_empty());
            let mut later = request.clone();
            later["age_seconds"] = serde_json::json!(14 * 86400);
            let mut child = command(root, env!("CARGO_BIN_EXE_pg-reap"))
                .arg("finish-attempt")
                .stdin(std::process::Stdio::piped())
                .spawn()
                .unwrap();
            child
                .stdin
                .take()
                .unwrap()
                .write_all(&serde_json::to_vec(&later).unwrap())
                .unwrap();
            assert!(child.wait().unwrap().success());
            assert!(!command(root, "git")
                .arg("-C")
                .arg(&repo)
                .args(["show-ref", "--verify", "--quiet", "refs/heads/lane/test"])
                .status()
                .unwrap()
                .success());
        }
        if outcome == "succeeded" {
            assert!(!git(root, &remote, &["rev-parse", "refs/heads/lane/test"]).is_empty());
        }
    }
}

fn attempt(
    outcome: pg_reap::retention::Outcome,
) -> (tempfile::TempDir, pg_reap::retention::Request) {
    let temp = tempfile::tempdir().unwrap();
    let root = temp.path();
    let repo = root.join("repo");
    let remote = root.join("origin.git");
    fs::create_dir(&repo).unwrap();
    fs::create_dir(&remote).unwrap();
    git(root, &remote, &["init", "--bare", "--initial-branch=main"]);
    git(root, &repo, &["init", "--initial-branch=main"]);
    git(root, &repo, &["commit", "--allow-empty", "-m", "base"]);
    git(
        root,
        &repo,
        &["remote", "add", "origin", remote.to_str().unwrap()],
    );
    git(root, &repo, &["push", "origin", "main"]);
    let host = root.join("attempts");
    fs::create_dir(&host).unwrap();
    let path = host.join("attempt-review");
    git(
        root,
        &repo,
        &[
            "worktree",
            "add",
            "-b",
            "lane/review",
            path.to_str().unwrap(),
        ],
    );
    (
        temp,
        pg_reap::retention::Request {
            repo,
            root: host,
            path,
            branch: "lane/review".into(),
            outcome,
            age_seconds: 14 * 86400,
            open_pr: false,
            live_claim: false,
        },
    )
}

#[test]
fn expired_unpushed_commit_is_refused_but_pushed_tip_can_be_deleted() {
    use pg_reap::retention::{execute, Action, Outcome};
    for outcome in [Outcome::Failed, Outcome::Cancelled] {
        let (temp, req) = attempt(outcome);
        git(
            temp.path(),
            &req.path,
            &["commit", "--allow-empty", "-m", "unpublished"],
        );
        let tip = git(temp.path(), &req.repo, &["rev-parse", "lane/review"]);
        let error = execute(&req).expect_err("unpublished tip must survive expiry");
        assert!(error.to_string().contains("unpushed"));
        assert!(req.path.exists());
        assert_eq!(
            git(temp.path(), &req.repo, &["rev-parse", "lane/review"]),
            tip
        );
        git(temp.path(), &req.repo, &["push", "origin", "lane/review"]);
        assert_eq!(execute(&req).unwrap(), Action::RemoveAndDeleteBranch);
        assert!(!req.path.exists());
        assert_eq!(
            git(
                temp.path(),
                &temp.path().join("origin.git"),
                &["rev-parse", "lane/review"]
            ),
            tip
        );
    }
}

#[test]
fn live_child_in_nested_cwd_refuses_terminal_cleanup() {
    use pg_reap::retention::{execute, Outcome};
    let (_temp, req) = attempt(Outcome::Failed);
    let nested = req.path.join("nested");
    fs::create_dir(&nested).unwrap();
    let mut child = Command::new("sleep")
        .arg("30")
        .current_dir(&nested)
        .spawn()
        .unwrap();
    // spawn() has completed the child's chdir before returning.
    let result = execute(&req);
    let alive = child.try_wait().unwrap().is_none();
    child.kill().ok();
    child.wait().unwrap();
    assert!(alive);
    let error = result.expect_err("live cwd must protect a terminal attempt");
    assert!(error.to_string().contains("in_use"));
    assert!(req.path.exists());
}

#[test]
fn branch_only_expiry_still_requires_a_published_tip() {
    use pg_reap::retention::{execute, Action, Outcome};
    let (temp, mut req) = attempt(Outcome::Failed);
    git(
        temp.path(),
        &req.path,
        &["commit", "--allow-empty", "-m", "unpublished"],
    );
    let tip = git(temp.path(), &req.repo, &["rev-parse", "lane/review"]);
    req.age_seconds = 1;
    assert_eq!(execute(&req).unwrap(), Action::RemoveAndKeepBranch);
    assert!(!req.path.exists());
    req.age_seconds = 14 * 86400;
    assert!(execute(&req).unwrap_err().to_string().contains("unpushed"));
    assert_eq!(
        git(temp.path(), &req.repo, &["rev-parse", "lane/review"]),
        tip
    );
    git(temp.path(), &req.repo, &["push", "origin", "lane/review"]);
    assert_eq!(execute(&req).unwrap(), Action::RemoveAndDeleteBranch);
}
