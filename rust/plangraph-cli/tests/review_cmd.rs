use serde_json::Value;
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::Command;
use tempfile::TempDir;

fn plangraph_bin() -> &'static str {
    env!("CARGO_BIN_EXE_plangraph")
}

fn repo_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("rust/")
        .parent()
        .expect("repo root")
        .to_path_buf()
}

fn git(repo: &Path, args: &[&str]) -> String {
    let out = Command::new("git")
        .arg("-C")
        .arg(repo)
        .args(args)
        .output()
        .expect("run git");
    assert!(
        out.status.success(),
        "git {:?} failed: {}",
        args,
        String::from_utf8_lossy(&out.stderr)
    );
    String::from_utf8_lossy(&out.stdout).trim().to_owned()
}

fn init_repo_with_origin() -> (TempDir, PathBuf, PathBuf, String) {
    let tmp = tempfile::tempdir().expect("tempdir");
    let bare = tmp.path().join("origin.git");
    let work = tmp.path().join("work");
    fs::create_dir_all(&bare).unwrap();
    let st = Command::new("git")
        .args(["init", "--bare", "-b", "main"])
        .arg(&bare)
        .status()
        .unwrap();
    assert!(st.success());

    let st = Command::new("git")
        .args(["clone"])
        .arg(&bare)
        .arg(&work)
        .status()
        .unwrap();
    assert!(st.success());

    git(&work, &["checkout", "-b", "main"]);
    git(&work, &["config", "user.name", "Test Bot"]);
    git(&work, &["config", "user.email", "test@example.com"]);

    fs::write(work.join("fixture-seed.txt"), "seed\n").unwrap();
    git(&work, &["add", "fixture-seed.txt"]);
    git(&work, &["commit", "-m", "seed\n\nWriter: gemini run-0"]);
    git(&work, &["push", "-u", "origin", "main"]);
    let base_sha = git(&work, &["rev-parse", "HEAD"]);
    (tmp, work, bare, base_sha)
}

fn write_mock_spawner(dir: &Path, state_dir: &Path, log_path: &Path, mode: &str) -> PathBuf {
    let script = dir.join("mock_spawner.py");
    let body = format!(
        r#"#!/usr/bin/env python3
import json, os, sys
from pathlib import Path

STATE = Path({state:?})
LOG = Path({log:?})
MODE = {mode:?}

args = sys.argv[1:]
def pop_flag(name):
    out = []
    i = 0
    vals = []
    while i < len(args):
        if args[i] == name and i + 1 < len(args):
            vals.append(args[i + 1])
            i += 2
        else:
            out.append(args[i])
            i += 1
    args[:] = out
    return vals

_ = pop_flag("--state")
verb = args[0]
rest = args[1:]

def parse_opts(items):
    opts = {{"writer_family": []}}
    i = 0
    while i < len(items):
        k = items[i]
        if k in ("--card", "--role", "--prompt-file", "--cwd", "--wait"):
            opts[k[2:].replace("-", "_")] = items[i + 1]
            i += 2
        elif k == "--writer-family":
            opts["writer_family"].append(items[i + 1])
            i += 2
        elif k in ("--json", "--dry-run", "--seats"):
            opts[k[2:].replace("-", "_")] = True
            i += 1
        else:
            i += 1
    return opts

opts = parse_opts(rest)
card = json.loads(Path(opts["card"][1:]).read_text()) if opts.get("card", "").startswith("@") else {{}}

with LOG.open("a", encoding="utf-8") as fh:
    fh.write(json.dumps({{"verb": verb, "writer_families": opts["writer_family"], "diff_ref": card.get("diff_ref")}}) + "\n")

if verb == "route":
    max_tok = 400 if MODE in ("split_pass", "split_fail_second", "single_too_big") else 200000
    if MODE == "null_leg":
        print(json.dumps({{"first_leg": None, "legs": []}}))
        sys.exit(0)
    print(json.dumps({{
        "first_leg": {{"id": "mimo-v2.6-pro", "family": "xiaomi", "harness": "opencode", "tool_mode": "inline-only", "max_request_tokens": max_tok}},
        "legs": [{{"id": "mimo-v2.6-pro", "family": "xiaomi", "harness": "opencode", "tool_mode": "inline-only", "max_request_tokens": max_tok}}],
    }}))
    sys.exit(0)

if verb == "spawn":
    idx = sum(1 for line in LOG.read_text().splitlines() if '"verb": "spawn"' in line)
    run_id = f"run-test-{{idx:04d}}"
    run_dir = STATE / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    head_sha = (card.get("diff_ref") or {{}}).get("head", "")
    live_log = run_dir / "live.log"

    if MODE == "split_fail_second" and idx == 2:
        reply = f"Found issue in second group.\nHEAD: {{head_sha}}\nTEST-GAMING: no\nVERDICT: REQUEST_CHANGES\n"
    elif MODE == "test_gaming_yes":
        reply = f"Looks fine.\nHEAD: {{head_sha}}\nTEST-GAMING: yes\nVERDICT: PASS\n"
    elif MODE == "redacted_head":
        reply = "Looks fine.\nHEAD: [REDACTED]\nTEST-GAMING: no\nVERDICT: PASS\n"
    elif MODE == "no_head":
        reply = "Looks fine.\nTEST-GAMING: no\nVERDICT: PASS\n"
    elif MODE == "seats_envelope":
        reply = f"Looks fine.\nHEAD: {{head_sha}}\nTEST-GAMING: no\nVERDICT: PASS\n"
    elif MODE == "fenced_pass":
        reply = "\n".join([
            json.dumps({{"type": "step_start", "part": {{}}}}),
            json.dumps({{"type": "text", "part": {{"text": f"Example block:\n```text\nVERDICT: REQUEST_CHANGES\n```\nVerified everything.\nHEAD: {{head_sha}}\nTEST-GAMING: no\nVERDICT: PASS"}}}}),
            json.dumps({{"type": "step_finish", "part": {{"reason": "stop"}}}}),
        ]) + "\n"
    else:
        reply = f"All clear.\nHEAD: {{head_sha}}\nTEST-GAMING: no\nVERDICT: PASS\n"

    live_log.write_text(reply, encoding="utf-8")
    rec = {{
        "run_id": run_id,
        "status": "completed",
        "exit_code": 0,
        "chosen_leg": {{"id": "mimo-v2.6-pro", "family": "xiaomi"}},
        "live_log": str(live_log),
        "stdout_tail": "" if MODE == "fenced_pass" else reply,
    }}
    (run_dir / "record.json").write_text(json.dumps(rec), encoding="utf-8")
    if MODE == "seats_envelope":
        print(json.dumps({{"seat_group": "seats-test", "seats": [rec]}}))
    else:
        print(json.dumps(rec))
    sys.exit(0)
"#,
        state = state_dir.display().to_string(),
        log = log_path.display().to_string(),
        mode = mode
    );
    fs::write(&script, body).unwrap();
    let mut perms = fs::metadata(&script).unwrap().permissions();
    perms.set_mode(0o755);
    fs::set_permissions(&script, perms).unwrap();
    script
}

#[test]
fn stale_head_is_refused() {
    let (tmp, work, bare, base_sha) = init_repo_with_origin();

    // Create a feature branch commit from base_sha.
    git(&work, &["checkout", "-b", "feat"]);
    fs::write(work.join("src_a.rs"), "pub fn a() -> i32 { 1 }\n").unwrap();
    git(&work, &["add", "src_a.rs"]);
    git(&work, &["commit", "-m", "feat\n\nWriter: gemini run-1"]);
    let feat_head = git(&work, &["rev-parse", "HEAD"]);

    // Advance origin/main in a second clone so feat_head is behind origin/main.
    let other = tmp.path().join("other");
    let st = Command::new("git")
        .args(["clone"])
        .arg(&bare)
        .arg(&other)
        .status()
        .unwrap();
    assert!(st.success());
    git(&other, &["config", "user.name", "Other"]);
    git(&other, &["config", "user.email", "other@example.com"]);
    fs::write(other.join("fixture-seed.txt"), "ahead\n").unwrap();
    git(&other, &["add", "fixture-seed.txt"]);
    git(&other, &["commit", "-m", "advance\n\nWriter: gpt run-2"]);
    git(&other, &["push", "origin", "main"]);

    let state = tmp.path().join("state");
    let log = tmp.path().join("spawner.jsonl");
    let mock = write_mock_spawner(tmp.path(), &state, &log, "pass");

    let out = Command::new(plangraph_bin())
        .args([
            "review",
            "--repo",
            work.to_str().unwrap(),
            "--base",
            &base_sha,
            "--head",
            &feat_head,
            "--state",
            state.to_str().unwrap(),
        ])
        .env("PLANGRAPH_SPAWNER_CMD", &mock)
        .env("PYTHONPATH", repo_root().join("src"))
        .output()
        .unwrap();

    assert_eq!(out.status.code(), Some(2));
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(stderr.contains("base_stale"), "stderr={stderr}");
    assert!(!log.exists(), "spawner should not be called on stale head");
}

#[test]
fn writer_families_extracted_from_trailers_and_fallback() {
    let (tmp, work, _bare, base_sha) = init_repo_with_origin();

    git(&work, &["checkout", "-b", "feat"]);

    // Commit 1: Author says Claude, Writer trailer says gemini -> must resolve ONLY google!
    git(&work, &["config", "user.name", "Claude Opus 5.5"]);
    fs::write(work.join("one.rs"), "pub fn one() {}\n").unwrap();
    git(&work, &["add", "one.rs"]);
    git(
        &work,
        &[
            "commit",
            "-m",
            "c1\n\nWriter: gemini-4-hydrogen-high run-abc",
        ],
    );

    // Commit 2: Hand-Edit mentions codex (openai) + non-family changelog note ignored.
    git(&work, &["config", "user.name", "Human User"]);
    fs::write(work.join("two.rs"), "pub fn two() {}\n").unwrap();
    git(&work, &["add", "two.rs"]);
    git(
        &work,
        &[
            "commit",
            "-m",
            "c2\n\nHand-Edit: changelog\nHand-Edit: codex manual tweak",
        ],
    );

    // Commit 3: No Writer trailer, falls back to Co-authored-by: deepseek.
    fs::write(work.join("three.rs"), "pub fn three() {}\n").unwrap();
    git(&work, &["add", "three.rs"]);
    git(
        &work,
        &[
            "commit",
            "-m",
            "c3\n\nCo-authored-by: DeepSeek <ds@example.com>",
        ],
    );

    let head_sha = git(&work, &["rev-parse", "HEAD"]);
    let state = tmp.path().join("state");
    let log = tmp.path().join("spawner.jsonl");
    let mock = write_mock_spawner(tmp.path(), &state, &log, "fenced_pass");
    let approval = tmp.path().join("approval.txt");

    let out = Command::new(plangraph_bin())
        .args([
            "review",
            "--repo",
            work.to_str().unwrap(),
            "--base",
            &base_sha,
            "--head",
            &head_sha,
            "--item",
            "plangraph:REVIEW-CMD",
            "--state",
            state.to_str().unwrap(),
            "--approval",
            approval.to_str().unwrap(),
            "--no-fetch",
        ])
        .env("PLANGRAPH_SPAWNER_CMD", &mock)
        .env("PYTHONPATH", repo_root().join("src"))
        .output()
        .unwrap();

    assert_eq!(
        out.status.code(),
        Some(0),
        "stderr={}\nstdout={}",
        String::from_utf8_lossy(&out.stderr),
        String::from_utf8_lossy(&out.stdout)
    );

    let entries: Vec<Value> = fs::read_to_string(&log)
        .unwrap()
        .lines()
        .map(|l| serde_json::from_str::<Value>(l).unwrap())
        .collect();
    assert_eq!(entries.len(), 2); // route + spawn
    for entry in entries {
        let fams = entry["writer_families"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_str().unwrap())
            .collect::<Vec<_>>();
        assert_eq!(fams, vec!["deepseek", "google", "openai"]);
    }

    // Verify merge_batch_land.py check_approval accepts the generated approval file.
    let tools_dir = repo_root().join("tools");
    let py = Command::new("python3")
        .args([
            "-c",
            &format!(
                "import sys; sys.path.insert(0, {:?}); from merge_batch_land import check_approval; sys.exit(0 if check_approval({:?}, {:?}) is None else 1)",
                tools_dir.display().to_string(),
                approval.display().to_string(),
                head_sha
            ),
        ])
        .status()
        .unwrap();
    assert!(
        py.success(),
        "merge_batch_land.check_approval rejected approval:\n{}",
        fs::read_to_string(&approval).unwrap()
    );
}

#[test]
fn oversize_diff_splits_across_seats_and_single_oversize_file_refuses() {
    let (tmp, work, _bare, base_sha) = init_repo_with_origin();
    git(&work, &["checkout", "-b", "feat"]);
    fs::create_dir_all(work.join("alpha")).unwrap();
    fs::create_dir_all(work.join("beta")).unwrap();
    fs::create_dir_all(work.join("gamma")).unwrap();

    // 400 tokens -> leg max chars = 1200; combined diff across 3 files > 1200 chars, each file < 550 chars.
    fs::write(work.join("alpha/a.rs"), format!("// {}\n", "a".repeat(260))).unwrap();
    fs::write(work.join("beta/b.rs"), format!("// {}\n", "b".repeat(260))).unwrap();
    fs::write(work.join("gamma/c.rs"), format!("// {}\n", "c".repeat(260))).unwrap();
    git(&work, &["add", "."]);
    git(&work, &["commit", "-m", "split\n\nWriter: gemini run-1"]);
    let head_sha = git(&work, &["rev-parse", "HEAD"]);

    let state = tmp.path().join("state");
    let log = tmp.path().join("spawner.jsonl");
    let mock = write_mock_spawner(tmp.path(), &state, &log, "split_pass");
    let approval = tmp.path().join("approval-split.txt");

    let out = Command::new(plangraph_bin())
        .args([
            "review",
            "--repo",
            work.to_str().unwrap(),
            "--base",
            &base_sha,
            "--head",
            &head_sha,
            "--state",
            state.to_str().unwrap(),
            "--approval",
            approval.to_str().unwrap(),
            "--no-fetch",
        ])
        .env("PLANGRAPH_SPAWNER_CMD", &mock)
        .env("PYTHONPATH", repo_root().join("src"))
        .output()
        .unwrap();
    assert_eq!(
        out.status.code(),
        Some(0),
        "stderr={}\nstdout={}",
        String::from_utf8_lossy(&out.stderr),
        String::from_utf8_lossy(&out.stdout)
    );

    let spawn_paths: Vec<Vec<String>> = fs::read_to_string(&log)
        .unwrap()
        .lines()
        .map(|l| serde_json::from_str::<Value>(l).unwrap())
        .filter(|v| v["verb"] == "spawn")
        .map(|v| {
            v["diff_ref"]["paths"]
                .as_array()
                .unwrap()
                .iter()
                .map(|p| p.as_str().unwrap().to_owned())
                .collect()
        })
        .collect();
    assert!(
        spawn_paths.len() >= 2,
        "expected split seats, got {spawn_paths:?}"
    );
    let mut covered: Vec<String> = spawn_paths.into_iter().flatten().collect();
    covered.sort();
    assert_eq!(covered, vec!["alpha/a.rs", "beta/b.rs", "gamma/c.rs"]);

    // Now test a split where group 2 returns REQUEST_CHANGES -> must fail and write NO approval.
    let approval_fail = tmp.path().join("approval-fail.txt");
    let mock_fail = write_mock_spawner(tmp.path(), &state, &log, "split_fail_second");
    fs::write(&log, "").unwrap();
    let out_fail = Command::new(plangraph_bin())
        .args([
            "review",
            "--repo",
            work.to_str().unwrap(),
            "--base",
            &base_sha,
            "--head",
            &head_sha,
            "--state",
            state.to_str().unwrap(),
            "--approval",
            approval_fail.to_str().unwrap(),
            "--no-fetch",
        ])
        .env("PLANGRAPH_SPAWNER_CMD", &mock_fail)
        .env("PYTHONPATH", repo_root().join("src"))
        .output()
        .unwrap();
    assert_eq!(out_fail.status.code(), Some(1));
    assert!(!approval_fail.exists());

    // Now test a single file larger than leg limit (1200 chars) -> refused naming the file.
    fs::write(
        work.join("alpha/huge.rs"),
        format!("// {}\n", "z".repeat(1400)),
    )
    .unwrap();
    git(&work, &["add", "alpha/huge.rs"]);
    git(&work, &["commit", "-m", "huge\n\nWriter: gemini run-2"]);
    let huge_head = git(&work, &["rev-parse", "HEAD"]);
    fs::write(&log, "").unwrap();
    let mock_huge = write_mock_spawner(tmp.path(), &state, &log, "single_too_big");
    let out_huge = Command::new(plangraph_bin())
        .args([
            "review",
            "--repo",
            work.to_str().unwrap(),
            "--base",
            &base_sha,
            "--head",
            &huge_head,
            "--state",
            state.to_str().unwrap(),
            "--no-fetch",
        ])
        .env("PLANGRAPH_SPAWNER_CMD", &mock_huge)
        .env("PYTHONPATH", repo_root().join("src"))
        .output()
        .unwrap();
    assert_eq!(out_huge.status.code(), Some(2));
    let stderr_huge = String::from_utf8_lossy(&out_huge.stderr);
    assert!(
        stderr_huge.contains("alpha/huge.rs"),
        "stderr={stderr_huge}"
    );
    assert!(
        !fs::read_to_string(&log).unwrap().contains("\"spawn\""),
        "must refuse before spawning seats"
    );
}

#[test]
fn test_gaming_yes_blocks_pass_even_with_verdict_pass() {
    let (tmp, work, _bare, base_sha) = init_repo_with_origin();
    git(&work, &["checkout", "-b", "feat"]);
    fs::write(work.join("f.rs"), "pub fn f() {}\n").unwrap();
    git(&work, &["add", "f.rs"]);
    git(&work, &["commit", "-m", "f\n\nWriter: gemini run-1"]);
    let head_sha = git(&work, &["rev-parse", "HEAD"]);

    let state = tmp.path().join("state");
    let log = tmp.path().join("spawner.jsonl");
    let mock = write_mock_spawner(tmp.path(), &state, &log, "test_gaming_yes");
    let approval = tmp.path().join("approval-gaming.txt");

    let out = Command::new(plangraph_bin())
        .args([
            "review",
            "--repo",
            work.to_str().unwrap(),
            "--base",
            &base_sha,
            "--head",
            &head_sha,
            "--state",
            state.to_str().unwrap(),
            "--approval",
            approval.to_str().unwrap(),
            "--no-fetch",
        ])
        .env("PLANGRAPH_SPAWNER_CMD", &mock)
        .env("PYTHONPATH", repo_root().join("src"))
        .output()
        .unwrap();

    assert_eq!(out.status.code(), Some(1));
    let stdout = String::from_utf8_lossy(&out.stdout);
    assert!(stdout.contains("test_gaming=yes"), "stdout={stdout}");
    assert!(
        stdout.contains("verdict=REQUEST_CHANGES"),
        "stdout={stdout}"
    );
    assert!(!approval.exists());
}

#[test]
fn redacted_head_line_blocks_pass_and_seats_envelope_extracts_records() {
    let (tmp, work, _bare, base_sha) = init_repo_with_origin();
    git(&work, &["checkout", "-b", "feat"]);
    fs::write(work.join("f.rs"), "pub fn f() {}\n").unwrap();
    git(&work, &["add", "f.rs"]);
    git(&work, &["commit", "-m", "f\n\nWriter: gemini run-1"]);
    let head_sha = git(&work, &["rev-parse", "HEAD"]);

    let state = tmp.path().join("state");
    let log = tmp.path().join("spawner.jsonl");
    let approval = tmp.path().join("approval-redacted.txt");
    let mock_redacted = write_mock_spawner(tmp.path(), &state, &log, "redacted_head");
    let out_redacted = Command::new(plangraph_bin())
        .args([
            "review",
            "--repo",
            work.to_str().unwrap(),
            "--base",
            &base_sha,
            "--head",
            &head_sha,
            "--state",
            state.to_str().unwrap(),
            "--approval",
            approval.to_str().unwrap(),
            "--no-fetch",
        ])
        .env("PLANGRAPH_SPAWNER_CMD", &mock_redacted)
        .env("PYTHONPATH", repo_root().join("src"))
        .output()
        .unwrap();
    assert_eq!(out_redacted.status.code(), Some(1));
    assert!(!approval.exists());

    let approval_seats = tmp.path().join("approval-seats.txt");
    let mock_seats = write_mock_spawner(tmp.path(), &state, &log, "seats_envelope");
    let out_seats = Command::new(plangraph_bin())
        .args([
            "review",
            "--repo",
            work.to_str().unwrap(),
            "--base",
            &base_sha,
            "--head",
            &head_sha,
            "--state",
            state.to_str().unwrap(),
            "--approval",
            approval_seats.to_str().unwrap(),
            "--seats",
            "--no-fetch",
        ])
        .env("PLANGRAPH_SPAWNER_CMD", &mock_seats)
        .env("PYTHONPATH", repo_root().join("src"))
        .output()
        .unwrap();
    assert_eq!(
        out_seats.status.code(),
        Some(0),
        "stderr={}\nstdout={}",
        String::from_utf8_lossy(&out_seats.stderr),
        String::from_utf8_lossy(&out_seats.stdout)
    );
    let body = fs::read_to_string(&approval_seats).unwrap();
    assert!(body.contains(&format!("HEAD: {head_sha}\nVERDICT: PASS")));
}

fn review_with_mode(
    mode: &str,
    files: &[(&str, &[u8])],
) -> (std::process::Output, PathBuf, PathBuf, TempDir) {
    let (tmp, work, _bare, base_sha) = init_repo_with_origin();
    git(&work, &["checkout", "-b", "feat"]);
    for (name, body) in files {
        fs::write(work.join(name), body).unwrap();
        git(&work, &["add", name]);
    }
    git(&work, &["commit", "-m", "f\n\nWriter: gemini run-1"]);
    let head_sha = git(&work, &["rev-parse", "HEAD"]);
    let state = tmp.path().join("state");
    let log = tmp.path().join("spawner.jsonl");
    let approval = tmp.path().join("approval.txt");
    let mock = write_mock_spawner(tmp.path(), &state, &log, mode);
    let out = Command::new(plangraph_bin())
        .args([
            "review",
            "--repo",
            work.to_str().unwrap(),
            "--base",
            &base_sha,
            "--head",
            &head_sha,
        ])
        .args([
            "--state",
            state.to_str().unwrap(),
            "--approval",
            approval.to_str().unwrap(),
            "--no-fetch",
        ])
        .env("PLANGRAPH_SPAWNER_CMD", &mock)
        .env("PYTHONPATH", repo_root().join("src"))
        .output()
        .unwrap();
    (out, approval, log, tmp)
}

#[test]
fn reply_without_head_line_is_not_a_pass() {
    let (out, approval, _log, _tmp) = review_with_mode("no_head", &[("f.rs", b"pub fn f() {}\n")]);
    assert_eq!(
        out.status.code(),
        Some(1),
        "stdout={}",
        String::from_utf8_lossy(&out.stdout)
    );
    assert!(!approval.exists());
}

#[test]
fn route_without_legs_is_refused_before_any_seat() {
    let (out, approval, log, _tmp) = review_with_mode("null_leg", &[("f.rs", b"pub fn f() {}\n")]);
    assert_eq!(out.status.code(), Some(2));
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(
        stderr.contains("no eligible review legs"),
        "stderr={stderr}"
    );
    assert!(!approval.exists());
    assert!(!fs::read_to_string(&log).unwrap().contains("\"spawn\""));
}

fn dry_run_review(
    work: &Path,
    tmp: &Path,
    base: &str,
    head: &str,
) -> (std::process::Output, PathBuf) {
    let state = tmp.join("state");
    let log = tmp.join("spawner.jsonl");
    let mock = write_mock_spawner(tmp, &state, &log, "split_pass");
    let out = Command::new(plangraph_bin())
        .args([
            "review",
            "--repo",
            work.to_str().unwrap(),
            "--base",
            base,
            "--head",
            head,
        ])
        .args(["--state", state.to_str().unwrap(), "--no-fetch"])
        .env("PLANGRAPH_SPAWNER_CMD", &mock)
        .env("PYTHONPATH", repo_root().join("src"))
        .output()
        .unwrap();
    (out, log)
}

#[test]
fn rename_old_path_is_in_a_group_like_the_spawner_lists_it() {
    let (tmp, work, _bare, _base) = init_repo_with_origin();
    git(&work, &["checkout", "-b", "feat"]);
    fs::create_dir_all(work.join("a")).unwrap();
    fs::create_dir_all(work.join("b")).unwrap();
    fs::create_dir_all(work.join("c")).unwrap();
    fs::write(work.join("a/old.rs"), format!("// {}\n", "r".repeat(260))).unwrap();
    git(&work, &["add", "."]);
    git(&work, &["commit", "-m", "old\n\nWriter: gemini run-1"]);
    let base = git(&work, &["rev-parse", "HEAD"]);
    git(&work, &["mv", "a/old.rs", "b/new.rs"]);
    fs::write(work.join("c/x.rs"), format!("// {}\n", "x".repeat(260))).unwrap();
    git(&work, &["add", "-A"]);
    git(&work, &["commit", "-m", "mv\n\nWriter: gemini run-2"]);
    let head = git(&work, &["rev-parse", "HEAD"]);
    let (out, log) = dry_run_review(&work, tmp.path(), &base, &head);
    assert!(
        out.status.success(),
        "stderr={}",
        String::from_utf8_lossy(&out.stderr)
    );
    let logged = fs::read_to_string(&log).unwrap();
    assert!(
        logged.contains("a/old.rs") && logged.contains("b/new.rs"),
        "{logged}"
    );
}

#[test]
fn binary_file_is_measured_as_the_inlined_base85_patch() {
    let (tmp, work, _bare, base) = init_repo_with_origin();
    git(&work, &["checkout", "-b", "feat"]);
    let bytes: Vec<u8> = (0..2000u32)
        .map(|i| (i.wrapping_mul(2654435761) >> 13) as u8)
        .collect();
    fs::write(work.join("blob.bin"), bytes).unwrap();
    fs::write(work.join("small.rs"), "pub fn s() {}\n").unwrap();
    git(&work, &["add", "-A"]);
    git(&work, &["commit", "-m", "bin\n\nWriter: gemini run-1"]);
    let head = git(&work, &["rev-parse", "HEAD"]);
    let (out, log) = dry_run_review(&work, tmp.path(), &base, &head);
    // A "Binary files differ" stub would fit the 1200-char limit; the base85 patch does not.
    assert_eq!(
        out.status.code(),
        Some(2),
        "stderr={}",
        String::from_utf8_lossy(&out.stderr)
    );
    assert!(String::from_utf8_lossy(&out.stderr).contains("blob.bin"));
    assert!(!fs::read_to_string(&log).unwrap().contains("\"spawn\""));
}
