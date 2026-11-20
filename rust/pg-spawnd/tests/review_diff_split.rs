#![cfg(unix)]
#[path = "common/binary.rs"]
mod binary;
mod common;
#[path = "common/placement.rs"]
#[allow(dead_code)]
mod placement;
use common::{ChildGuard, McpClient, TempDir};
use placement::payload;
use serde_json::{json, Value};
use std::os::unix::fs::PermissionsExt;

struct ReviewFixture {
    state: TempDir,
    repo: std::path::PathBuf,
    _guard: ChildGuard,
    client: McpClient,
}

impl ReviewFixture {
    fn new(max_request_tokens: u64, command: &str) -> Self {
        let state = TempDir::new();
        let repo = state.path().join("repo");
        std::fs::create_dir_all(&repo).unwrap();
        let run_git = |args: &[&str]| {
            let out = std::process::Command::new("git")
                .current_dir(&repo)
                .env_remove("GIT_DIR")
                .env_remove("GIT_WORK_TREE")
                .args(args)
                .output()
                .unwrap();
            assert!(
                out.status.success(),
                "{args:?}: {}",
                String::from_utf8_lossy(&out.stderr)
            );
        };
        run_git(&["init", "-q"]);
        run_git(&["config", "user.name", "Fixture"]);
        run_git(&["config", "user.email", "fixture@example.invalid"]);
        std::fs::write(repo.join("init.txt"), "base\n").unwrap();
        run_git(&["add", "init.txt"]);
        run_git(&["commit", "-qm", "base"]);

        let leg = json!({
            "id": "mimo-v2.6-pro",
            "harness": "opencode",
            "model": "mimo/mimo-v2.6-pro",
            "family": "mimo",
            "tool_mode": "inline-only",
            "max_request_tokens": max_request_tokens
        });
        let plan = json!({"legs": [leg.clone()], "first_leg": leg, "skipped": []});
        let route = state.path().join("route");
        std::fs::write(
            &route,
            format!(
                "#!/bin/sh\nprintf '%s' '{}'\n",
                plan.to_string().replace('\'', "'\\''")
            ),
        )
        .unwrap();
        std::fs::set_permissions(&route, std::fs::Permissions::from_mode(0o755)).unwrap();

        let launch = state.path().join("launch");
        let spec = json!({
            "argv": ["/bin/sh", "-c", command],
            "cwd": state.path(),
            "env": {},
            "prompt": "review",
            "stdin": "review"
        });
        std::fs::write(
            &launch,
            format!(
                "#!/bin/sh\ncat >/dev/null\nprintf '%s' '{}'\n",
                spec.to_string().replace('\'', "'\\''")
            ),
        )
        .unwrap();
        std::fs::set_permissions(&launch, std::fs::Permissions::from_mode(0o755)).unwrap();

        let cfg_dir = state.path().join("xdg/plangraph");
        std::fs::create_dir_all(&cfg_dir).unwrap();
        let hosts = json!({
            "version": 1,
            "self": "local",
            "hosts": [{
                "id": "local",
                "class": "worker",
                "aliases": [],
                "transport": "ssh",
                "os": "linux",
                "state_dir": state.path(),
                "caps": {"max_runs": 8, "max_writers": 2, "max_seats": 4, "max_sessions": 4, "mem_floor_mb": 1024},
                "warmup": {"warmup_s": 1, "ramp_mb": {"review": 0}},
                "session_mb": 256,
                "roles": ["review"],
                "repos": {
                    "demo": {
                        "root": repo,
                        "worktrees_root": state.path().join("wt"),
                        "remote": "https://example.invalid/demo.git",
                        "import_roots": [],
                        "cache_env": {}
                    }
                }
            }]
        });
        std::fs::write(cfg_dir.join("hosts.json"), hosts.to_string()).unwrap();

        let socket = state.path().join("socket");
        let mut cmd = std::process::Command::new(binary::spawnd());
        cmd.args(["serve", "--no-scope", "--socket"])
            .arg(&socket)
            .arg("--state")
            .arg(state.path())
            .env("XDG_CONFIG_HOME", state.path().join("xdg"))
            .env("PG_SPAWND_ADAPTER", &route)
            .env("PG_SPAWND_LAUNCH_CMD", &launch);
        let child = cmd.spawn().unwrap();
        let guard = ChildGuard { child };
        let mut client = McpClient::connect(&socket);
        client.initialize();
        Self {
            state,
            repo,
            _guard: guard,
            client,
        }
    }

    fn git(&self, args: &[&str]) -> String {
        let out = std::process::Command::new("git")
            .current_dir(&self.repo)
            .env_remove("GIT_DIR")
            .env_remove("GIT_WORK_TREE")
            .args(args)
            .output()
            .unwrap();
        assert!(
            out.status.success(),
            "{args:?}: {}",
            String::from_utf8_lossy(&out.stderr)
        );
        String::from_utf8_lossy(&out.stdout).trim().into()
    }

    fn git_bytes(&self, args: &[&str]) -> Vec<u8> {
        let out = std::process::Command::new("git")
            .current_dir(&self.repo)
            .env_remove("GIT_DIR")
            .env_remove("GIT_WORK_TREE")
            .args(args)
            .output()
            .unwrap();
        assert!(out.status.success());
        out.stdout
    }

    fn spawn_review(&mut self, diff_ref: Value) -> Value {
        self.client.request(
            "tools/call",
            json!({
                "name": "plangraph_agent_spawn",
                "arguments": {
                    "role": "review",
                    "prompt": "review",
                    "card": {"diff_ref": diff_ref},
                    "wait_seconds": 5
                }
            }),
        )
    }
}

fn extract_inline_diff(prompt: &str) -> &str {
    let start = prompt.find("```diff\n").expect("diff fence open") + "```diff\n".len();
    let end = prompt[start..]
        .find("\n```\n<!-- PLANGRAPH INLINE DIFF END -->")
        .expect("diff fence close");
    &prompt[start..start + end]
}

#[test]
fn diff_ref_with_paths_inlines_only_requested_paths() {
    let mut f = ReviewFixture::new(
        5000,
        r#"cat >/dev/null; printf '%s\n' '{"type":"result","is_error":false,"result":"VERDICT: ACCEPT\n"}'"#,
    );
    let base = f.git(&["rev-parse", "HEAD"]);
    std::fs::create_dir_all(f.repo.join("src")).unwrap();
    std::fs::create_dir_all(f.repo.join("docs")).unwrap();
    std::fs::write(f.repo.join("src/keep.rs"), "fn keep() {}\n").unwrap();
    std::fs::write(f.repo.join("docs/skip.md"), "ignored doc change\n").unwrap();
    f.git(&["add", "."]);
    f.git(&["commit", "-qm", "changes"]);
    let head = f.git(&["rev-parse", "HEAD"]);

    let reply = f.spawn_review(json!({
        "repo_id": "demo",
        "base_sha": base,
        "head_sha": head,
        "paths": ["src/keep.rs"]
    }));
    let row = payload(reply);
    assert_eq!(row["status"], "completed", "{row}");
    let run_id = row["run_id"].as_str().unwrap();
    let prompt =
        std::fs::read_to_string(f.state.path().join("runs").join(run_id).join("prompt.txt"))
            .unwrap();
    assert!(prompt.contains("fn keep()"), "{prompt}");
    assert!(!prompt.contains("ignored doc change"), "{prompt}");
}

#[test]
fn invalid_diff_ref_paths_are_refused_without_launching() {
    let marker = std::env::temp_dir().join(pg_worker_core::run::record::new_run_id());
    let cmd = format!("touch '{}'", marker.display());
    let mut f = ReviewFixture::new(5000, &cmd);
    let base = f.git(&["rev-parse", "HEAD"]);
    std::fs::write(f.repo.join("a.txt"), "hello\n").unwrap();
    f.git(&["add", "a.txt"]);
    f.git(&["commit", "-qm", "c"]);
    let head = f.git(&["rev-parse", "HEAD"]);

    for bad in [
        json!(["/etc/passwd"]),
        json!(["../secret"]),
        json!(["sub/../secret"]),
        json!(["--output=/tmp/evil"]),
        json!([]),
    ] {
        let reply = f.spawn_review(json!({
            "repo_id": "demo",
            "base_sha": base,
            "head_sha": head,
            "paths": bad
        }));
        assert!(
            reply.get("error").is_some(),
            "expected refusal for {bad}: {reply}"
        );
    }
    assert!(!marker.exists(), "invalid paths must not launch");
}

#[test]
fn oversize_diff_splits_into_group_seats_covering_full_diff_and_fitting_limit() {
    let mut f = ReviewFixture::new(
        220, // 660 chars max per seat prompt
        r#"cat >/dev/null; printf '%s\n' '{"type":"result","is_error":false,"result":"Reviewed.\nVERDICT: ACCEPT\n"}'"#,
    );
    let base = f.git(&["rev-parse", "HEAD"]);
    std::fs::create_dir_all(f.repo.join("alpha")).unwrap();
    std::fs::create_dir_all(f.repo.join("beta")).unwrap();
    std::fs::create_dir_all(f.repo.join("gamma")).unwrap();
    // alpha has two 180-char files: together > 660, each alone fits < 660.
    std::fs::write(
        f.repo.join("alpha/one.txt"),
        format!("1:{}\n", "line-one ".repeat(20)),
    )
    .unwrap();
    std::fs::write(
        f.repo.join("alpha/two.txt"),
        format!("2:{}\n", "line-two ".repeat(20)),
    )
    .unwrap();
    // beta and gamma are small and should pack together.
    std::fs::write(f.repo.join("beta/b.txt"), "beta\n").unwrap();
    std::fs::write(f.repo.join("gamma/g.txt"), "gamma\n").unwrap();
    f.git(&["add", "."]);
    f.git(&["commit", "-qm", "multi"]);
    let head = f.git(&["rev-parse", "HEAD"]);

    let reply = f.spawn_review(json!({
        "repo_id": "demo",
        "base_sha": base,
        "head_sha": head
    }));
    let row = payload(reply);
    assert_eq!(row["status"], "completed", "{row}");
    assert_eq!(row["verdict"], "PASS", "{row}");
    assert_eq!(row["passed"], true, "{row}");
    let seats = row["seats"].as_array().expect("seats array");
    assert_eq!(seats.len(), 3, "expected 3 packed groups: {row}");

    let mut seen_paths = std::collections::BTreeSet::new();
    let mut combined_diff = String::new();
    for (idx, seat) in seats.iter().enumerate() {
        let run_id = seat["run_id"].as_str().unwrap();
        let prompt =
            std::fs::read_to_string(f.state.path().join("runs").join(run_id).join("prompt.txt"))
                .unwrap();
        assert!(
            prompt.chars().count() <= 220 * 3,
            "seat {} exceeded prompt limit: {} chars",
            idx + 1,
            prompt.chars().count()
        );
        let group_paths: Vec<String> = seat["review_group"]["paths"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_str().unwrap().to_owned())
            .collect();
        assert!(
            prompt.contains(&format!("{}/{}", idx + 1, seats.len())),
            "prompt missing index/total: {prompt}"
        );
        for p in &group_paths {
            assert!(prompt.contains(p), "prompt missing path {p}: {prompt}");
            assert!(seen_paths.insert(p.clone()), "duplicate path {p}");
        }
        combined_diff.push_str(extract_inline_diff(&prompt));
        if !combined_diff.ends_with('\n') {
            combined_diff.push('\n');
        }
    }
    let expected_paths: std::collections::BTreeSet<String> = [
        "alpha/one.txt",
        "alpha/two.txt",
        "beta/b.txt",
        "gamma/g.txt",
    ]
    .into_iter()
    .map(str::to_owned)
    .collect();
    assert_eq!(seen_paths, expected_paths);

    // Every changed file's diff from the full diff appears identically in combined_diff.
    let full_diff = String::from_utf8(f.git_bytes(&[
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--binary",
        &base,
        &head,
        "--",
    ]))
    .unwrap();
    let mut actual_chunks: Vec<&str> = combined_diff
        .split("diff --git ")
        .filter(|s| !s.is_empty())
        .collect();
    let mut expected_chunks: Vec<&str> = full_diff
        .split("diff --git ")
        .filter(|s| !s.is_empty())
        .collect();
    actual_chunks.sort();
    expected_chunks.sort();
    assert_eq!(actual_chunks, expected_chunks);
}

#[test]
fn single_file_over_limit_refuses_naming_file() {
    let mut f = ReviewFixture::new(
        100,
        r#"cat >/dev/null; touch should_not_launch; printf '{"type":"result","is_error":false,"result":"VERDICT: ACCEPT\n"}\n'"#,
    );
    let base = f.git(&["rev-parse", "HEAD"]);
    std::fs::create_dir_all(f.repo.join("pkg")).unwrap();
    std::fs::write(f.repo.join("pkg/small.txt"), "ok\n").unwrap();
    std::fs::write(
        f.repo.join("pkg/giant_file.rs"),
        format!("{}\n", "x".repeat(500)),
    )
    .unwrap();
    f.git(&["add", "."]);
    f.git(&["commit", "-qm", "giant"]);
    let head = f.git(&["rev-parse", "HEAD"]);

    let reply = f.spawn_review(json!({
        "repo_id": "demo",
        "base_sha": base,
        "head_sha": head
    }));
    let row = payload(reply);
    assert_eq!(row["status"], "failed", "{row}");
    assert_eq!(row["reason"], "prompt_too_large", "{row}");
    let err = row["error"].as_str().unwrap();
    assert!(
        err.contains("pkg/giant_file.rs"),
        "expected error to name oversized file: {err}"
    );
    assert!(!f.state.path().join("should_not_launch").exists());
}

#[test]
fn group_request_changes_fails_review() {
    let cmd = r#"
input=$(cat)
if printf '%s' "$input" | grep -q 'beta/bad.txt'; then
  printf '%s\n' '{"type":"result","is_error":false,"result":"Bug in beta/bad.txt.\nVERDICT: REQUEST_CHANGES\n"}'
else
  printf '%s\n' '{"type":"result","is_error":false,"result":"LGTM.\nVERDICT: ACCEPT\n"}'
fi
"#;
    let mut f = ReviewFixture::new(220, cmd);
    let base = f.git(&["rev-parse", "HEAD"]);
    std::fs::create_dir_all(f.repo.join("alpha")).unwrap();
    std::fs::create_dir_all(f.repo.join("beta")).unwrap();
    std::fs::write(
        f.repo.join("alpha/good.txt"),
        format!("{}\n", "a".repeat(220)),
    )
    .unwrap();
    std::fs::write(
        f.repo.join("beta/bad.txt"),
        format!("{}\n", "b".repeat(220)),
    )
    .unwrap();
    f.git(&["add", "."]);
    f.git(&["commit", "-qm", "two-groups"]);
    let head = f.git(&["rev-parse", "HEAD"]);

    let reply = f.spawn_review(json!({
        "repo_id": "demo",
        "base_sha": base,
        "head_sha": head
    }));
    let row = payload(reply);
    assert_eq!(row["status"], "failed", "{row}");
    assert_eq!(row["verdict"], "REQUEST_CHANGES", "{row}");
    assert_eq!(row["passed"], false, "{row}");
    let seats = row["seats"].as_array().unwrap();
    assert_eq!(seats.len(), 2, "{row}");
}
