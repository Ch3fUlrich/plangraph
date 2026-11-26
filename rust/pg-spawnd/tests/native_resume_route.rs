//! The target-host control route resumes a native writer without a model seat.
#![cfg(unix)]

#[path = "common/binary.rs"]
mod binary;
#[path = "native_writer_fixture/codex.rs"]
mod codex_fixture;

use pg_worker_core::{executable_test_support::write_executable, git_test_support};
use serde_json::{json, Value};
use std::{
    fs,
    io::Write,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    time::{Duration, Instant},
};

const RUN: &str = "run-fixround";

fn git(path: &Path, args: &[&str]) -> String {
    let output = git_test_support::command(path).args(args).output().unwrap();
    assert!(
        output.status.success(),
        "{args:?}: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8(output.stdout).unwrap().trim().into()
}

struct Fixture {
    root: tempfile::TempDir,
    repo: PathBuf,
    directory: PathBuf,
}

impl Fixture {
    fn new() -> Self {
        let root = tempfile::tempdir().unwrap();
        let repo = root.path().join("repo");
        let remote = root.path().join("remote.git");
        fs::create_dir(&repo).unwrap();
        git(root.path(), &["init", "--bare", remote.to_str().unwrap()]);
        git(&repo, &["init", "-b", "main"]);
        git(&repo, &["commit", "--allow-empty", "-m", "base"]);
        git(
            &repo,
            &["remote", "add", "origin", remote.to_str().unwrap()],
        );
        git(&repo, &["push", "origin", "main"]);

        let bin = root.path().join("bin");
        fs::create_dir(&bin).unwrap();
        write_executable(bin.join("codex"), codex_fixture::CODEX);
        write_executable(
            bin.join("df"),
            "#!/bin/sh\nprintf 'fixture 0 0 10485760 0%% /\\n'\n",
        );
        write_executable(
            bin.join("plangraph"),
            format!(
                "#!/bin/sh\n{}",
                include_str!("fixtures/fake-plangraph-fast-checks.txt")
            ),
        );
        fs::write(
            root.path().join("review.py"),
            r#"import json, sys
card = json.loads(sys.argv[sys.argv.index('--card') + 1])
head = card['diff_ref']['head']
text = f'FILES READ: writer.txt\nTESTS: cargo test -p fixture — 1 passed\nHEAD: {head}\nVERDICT: ACCEPT\n'
print(json.dumps(dict(run_id='run-review', status='completed', head_sha=head, host='fixture-review',
                      overflow_remote=True, item_key=card['item_key'], stdout_tail=text,
                      chosen_leg=dict(id='claude-sonnet-5-5-high', harness='claude', tool_mode='tools'))))
"#,
        )
        .unwrap();
        let directory = root.path().join("state/runs").join(RUN);
        fs::create_dir_all(&directory).unwrap();
        let fixture = Self {
            root,
            repo,
            directory,
        };
        let record = json!({
            "run_id": RUN, "host": "fixture-host", "status": "running", "started": 1.0,
            "run_kind": "native-codex", "role": "implement", "repo": fixture.repo,
            "item_key": "demo:FIXTURE",
            "base_ref": "refs/heads/main", "tool_path": fixture.tool_path(),
            "review_dir": fixture.directory.join("reviews"),
        });
        let saved = json!({
            "record_path": fixture.directory.join("record.json"),
            "record": record, "prompt": "brief marker",
            "card": {
                "item_key": "demo:FIXTURE",
                "review_leg": "claude-sonnet-5-5-high",
                "review_override_reason": "D-6550 fixture review",
            },
        });
        let request = fixture.directory.join("native-request.json");
        fs::write(&request, saved.to_string()).unwrap();
        let output = fixture
            .command()
            .arg("native-writer")
            .arg(request)
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        let admitted = fixture.record();
        assert_eq!(admitted["status"], "completed", "{admitted:#}");
        assert_eq!(admitted["session_id"], "fixture-session", "{admitted:#}");
        fixture
    }

    fn tool_path(&self) -> String {
        format!("{}:/usr/bin:/bin", self.root.path().join("bin").display())
    }

    fn command(&self) -> Command {
        let mut cmd = Command::new(binary::spawnd());
        cmd.env_clear()
            .env("PATH", self.tool_path())
            .env("HOME", self.root.path())
            .env(
                "PYTHONPATH",
                Path::new(env!("CARGO_MANIFEST_DIR")).join("../../src"),
            )
            .env("PLANGRAPH_HOST_NAME", "fixture-host")
            .env(
                "PLANGRAPH_NATIVE_REVIEW_CLI",
                json!(["python3", self.root.path().join("review.py")]).to_string(),
            );
        git_test_support::configure(&mut cmd, self.root.path());
        cmd
    }

    fn record(&self) -> Value {
        serde_json::from_slice(&fs::read(self.directory.join("record.json")).unwrap()).unwrap()
    }

    fn request(&self) -> Value {
        let record = self.record();
        json!({
            "host": record["host"], "session_id": record["session_id"], "cwd": record["cwd"],
            "final_head": record["final_head"], "resume_number": 0,
            "prompt": "brief marker external fix",
        })
    }

    fn resume(&self, body: &Value) -> Value {
        let mut child = self
            .command()
            .args(["resume-local", RUN, "--state"])
            .arg(self.root.path().join("state"))
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        writeln!(child.stdin.take().unwrap(), "{body}").unwrap();
        let output = child.wait_with_output().unwrap();
        serde_json::from_slice(&output.stdout)
            .unwrap_or_else(|error| panic!("{error}: {}", String::from_utf8_lossy(&output.stderr)))
    }
}

#[test]
fn resumes_same_session_and_produces_new_commit() {
    let fixture = Fixture::new();
    let prior = fixture.record();
    let pins = fixture.request();
    let ack = fixture.resume(&pins);
    assert_eq!(ack["ok"], true, "{ack:#}");
    assert_eq!(ack["resume_number"], 1, "{ack:#}");
    let retry = fixture.resume(&pins);
    assert_eq!(retry, ack, "{retry:#}");

    let deadline = Instant::now() + Duration::from_secs(30);
    let end = loop {
        let row = fixture.record();
        if row["ended"].is_number() && row["resume_number"] == 1 {
            break row;
        }
        assert!(Instant::now() < deadline, "{row:#}");
        std::thread::sleep(Duration::from_millis(50));
    };
    assert_eq!(end["status"], "completed", "{end:#}");
    for key in ["run_id", "host", "session_id", "cwd"] {
        assert_eq!(end[key], prior[key], "{key}");
    }
    assert_eq!(end["resume_from"]["head"], prior["final_head"]);
    let cwd = Path::new(end["cwd"].as_str().unwrap());
    assert!(cwd.join(".git").is_file(), "expected a linked worktree");
    assert_eq!(git(cwd, &["rev-parse", "HEAD^"]), prior["final_head"]);
    assert_eq!(git(cwd, &["rev-parse", "HEAD"]), end["final_head"]);
    assert_eq!(git(cwd, &["log", "-1", "--format=%s"]), "fix");
}

#[test]
fn refuses_mismatched_pins_before_start() {
    let fixture = Fixture::new();
    let prior = fixture.record();
    let log = fs::read(fixture.directory.join("native.log")).unwrap();
    for (field, value) in [
        ("host", json!("other-host")),
        ("session_id", json!("other-session")),
        ("cwd", json!("/different/worktree")),
        ("final_head", json!("0".repeat(40))),
        ("resume_number", json!(9)),
        ("host", Value::Null),
        ("session_id", Value::Null),
        ("cwd", Value::Null),
    ] {
        let mut request = fixture.request();
        request[field] = value;
        let refused = fixture.resume(&request);
        assert_eq!(refused["ok"], false, "{field}: {refused:#}");
        assert_eq!(fixture.record(), prior, "{field}");
        assert_eq!(fs::read(fixture.directory.join("native.log")).unwrap(), log);
    }
    let cwd = Path::new(prior["cwd"].as_str().unwrap());
    assert_eq!(git(cwd, &["rev-parse", "HEAD"]), prior["final_head"]);
}
