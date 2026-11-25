//! The real `pg-spawnd step-run` process: killed during a step, then started again on the
//! same steps file. A fake `pg` (the keeper CLI) kills its parent on the first claim, so the
//! process dies with no row saved for it; the restarts show which actions run again.
#![cfg(unix)]
#[path = "common/binary.rs"]
mod binary;
use std::{fs, os::unix::fs::PermissionsExt, path::Path, process::Command};

fn git(dir: &Path, args: &[&str]) -> String {
    let out = pg_worker_core::git_test_support::command(dir)
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

fn private(path: &Path, text: &str) {
    fs::write(path, text).unwrap();
    fs::set_permissions(path, fs::Permissions::from_mode(0o600)).unwrap();
}

struct Fixture {
    root: tempfile::TempDir,
    base: String,
}

impl Fixture {
    fn new() -> Self {
        let root = tempfile::tempdir().unwrap();
        let (origin, repo) = (root.path().join("origin.git"), root.path().join("repo"));
        fs::create_dir_all(&origin).unwrap();
        fs::create_dir_all(&repo).unwrap();
        git(&origin, &["init", "-q", "--bare", "-b", "main"]);
        git(&repo, &["init", "-q", "-b", "main"]);
        git(&repo, &["commit", "-q", "--allow-empty", "-m", "base"]);
        git(
            &repo,
            &["remote", "add", "origin", origin.to_str().unwrap()],
        );
        git(&repo, &["push", "-q", "origin", "main"]);
        let base = git(&repo, &["rev-parse", "HEAD"]);
        // The keeper CLI: log every call; the first claim-batch kills the calling process.
        let bin = root.path().join("bin");
        fs::create_dir_all(&bin).unwrap();
        let pg = bin.join("pg");
        fs::write(
            &pg,
            "#!/bin/sh\necho \"$PG_IDEMPOTENCY_KEY $*\" >> \"$PG_LOG\"\n\
             if [ \"$(wc -l < \"$PG_LOG\")\" = 1 ]; then kill -9 $PPID; sleep 5; fi\n\
             printf '{\"results\":[{\"key\":\"demo:A\",\"epoch\":3}]}\\n'\n",
        )
        .unwrap();
        fs::set_permissions(&pg, fs::Permissions::from_mode(0o755)).unwrap();
        // The native run the subcommand reads: record and request of one writer run.
        let run = root.path().join("state/runs/run-abc");
        fs::create_dir_all(&run).unwrap();
        let record = serde_json::json!({
            "run_id": "run-abc", "run_kind": "native-codex", "repo": repo,
            "cwd": root.path().join("wt"), "base_ref": "refs/heads/main",
            "tool_path": format!("{}:/usr/bin:/bin", bin.display()),
        });
        private(&run.join("record.json"), &record.to_string());
        let request = serde_json::json!({
            "record": {"run_id": "run-abc"}, "prompt": "brief text", "card": {},
            "record_path": run.join("record.json"),
        });
        private(&run.join("native-request.json"), &request.to_string());
        private(&root.path().join("brief.md"), "# batch\n");
        Self { root, base }
    }

    fn path(&self, name: &str) -> std::path::PathBuf {
        self.root.path().join(name)
    }

    /// One start of the subcommand; its exit code and the JSON line it printed.
    fn start(&self) -> (Option<i32>, String) {
        let out = Command::new(binary::spawnd())
            .arg("step-run")
            .args(["run-abc", "--state", self.path("state").to_str().unwrap()])
            .args(["--steps", self.path("steps.json").to_str().unwrap()])
            .args([
                "--repo",
                "demo/repo",
                "--batch-key",
                "batch-1",
                "--items",
                "demo:A",
            ])
            .args([
                "--base",
                &self.base,
                "--brief",
                self.path("brief.md").to_str().unwrap(),
            ])
            .args(["--train-cmd", "train-desk join", "--ci-workflow", "CI"])
            .args(["--keeper-cmd", "pg --json", "--no-journal"])
            .env(
                "PATH",
                format!("{}:/usr/bin:/bin", self.path("bin").display()),
            )
            .env("PG_LOG", self.path("pg.log"))
            .env("HOME", self.root.path())
            .output()
            .unwrap();
        (
            out.status.code(),
            String::from_utf8_lossy(&out.stdout).into_owned(),
        )
    }

    fn claims(&self) -> usize {
        fs::read_to_string(self.path("pg.log")).map_or(0, |log| log.lines().count())
    }

    fn rows(&self) -> Vec<String> {
        let Ok(bytes) = fs::read(self.path("steps.json")) else {
            return Vec::new();
        };
        let saved: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        saved["rows"]
            .as_array()
            .unwrap()
            .iter()
            .map(|row| {
                format!(
                    "{}:{}",
                    row["step"]["type"].as_str().unwrap(),
                    row["step"]["result"]["status"].as_str().unwrap()
                )
            })
            .collect()
    }
}

#[test]
fn a_killed_step_run_resumes_from_its_steps_file() {
    let fixture = Fixture::new();

    // First start: the claim kills the process before its row is saved.
    let (code, _) = fixture.start();
    assert_eq!(code, None, "the process died of a signal");
    assert_eq!(fixture.claims(), 1);
    assert!(
        fixture.rows().is_empty(),
        "no row for the step that was killed"
    );

    // Second start: the unrecorded claim runs again and is recorded, then the list goes on to
    // the writer turn, which fails here (no codex): the process ends with the failing step.
    let (code, line) = fixture.start();
    assert_eq!(code, Some(1), "{line}");
    assert_eq!(fixture.claims(), 2);
    // The killed claim and its retry carry one idempotency key: a keeper that had granted the
    // first would answer the second from its receipt instead of refusing the keys.
    let log = fs::read_to_string(fixture.path("pg.log")).unwrap();
    let keys: Vec<&str> = log
        .lines()
        .filter_map(|line| line.split_whitespace().next())
        .collect();
    assert_eq!(keys[0], keys[1], "{log}");
    assert_eq!(keys[0].len(), 32, "{log}");
    let rows = fixture.rows();
    assert_eq!(
        &rows[..2],
        ["claim:completed", "worktree:completed"],
        "{rows:?}"
    );
    assert!(
        rows[2..].iter().all(|row| row == "write:failed"),
        "{rows:?}"
    );
    assert!(line.contains(r#""step":"write""#), "{line}");

    // Third start: claim and worktree come from the file; only the writer turn is tried.
    let before = rows.len();
    let (code, _) = fixture.start();
    assert_eq!(code, Some(1));
    assert_eq!(fixture.claims(), 2, "the recorded claim did not run again");
    assert_eq!(fixture.rows().len(), before + 1);
}
