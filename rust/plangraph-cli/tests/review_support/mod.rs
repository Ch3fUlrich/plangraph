#![allow(dead_code)]

use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::Command;
use tempfile::TempDir;

pub fn plangraph_bin() -> &'static str {
    env!("CARGO_BIN_EXE_plangraph")
}

pub fn repo_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("rust/")
        .parent()
        .expect("repo root")
        .to_path_buf()
}

pub fn git(repo: &Path, args: &[&str]) -> String {
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

pub fn init_repo_with_origin() -> (TempDir, PathBuf, PathBuf, String) {
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

pub fn write_mock_spawner(dir: &Path, state_dir: &Path, log_path: &Path, mode: &str) -> PathBuf {
    let script = dir.join("mock_spawner.py");
    let body = format!(
        r#"#!/usr/bin/env python3
import json, sys, tempfile
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
        elif k == "--exclude-family":
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

# The seat family is the first preferred family outside the writer families it was asked to exclude.
ORDER = ("google", "openai", "deepseek", "xiaomi") if MODE in ("family_order", "fenced_pass") else ("xiaomi",)
SEAT_FAMILY = next(f for f in ORDER if f not in opts["writer_family"])
with LOG.open("a", encoding="utf-8") as fh:
    fh.write(json.dumps({{"verb": verb, "writer_families": opts["writer_family"], "seat_family": SEAT_FAMILY, "diff_ref": card.get("diff_ref")}}) + "\n")

if verb == "route":
    max_tok = 400 if MODE in ("split_pass", "split_fail_beta", "single_too_big") else 200000
    if MODE == "null_leg":
        print(json.dumps({{"first_leg": None, "legs": []}}))
        sys.exit(0)
    print(json.dumps({{
        "first_leg": {{"id": "mimo-v2.6-pro", "family": SEAT_FAMILY, "harness": "opencode", "tool_mode": "inline-only", "max_request_tokens": max_tok}},
        "legs": [{{"id": "mimo-v2.6-pro", "family": SEAT_FAMILY, "harness": "opencode", "tool_mode": "inline-only", "max_request_tokens": max_tok}}],
    }}))
    sys.exit(0)

if verb == "spawn":
    runs = STATE / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix="run-test-", dir=runs))
    run_id = run_dir.name
    diff_ref = card.get("diff_ref") or {{}}
    head_sha = diff_ref.get("head", "")
    live_log = run_dir / "live.log"

    if (MODE == "split_fail_beta" and "beta/b.rs" in diff_ref.get("paths", [])
            or MODE == "fail_openai_group" and opts["writer_family"] == ["openai"]):
        reply = f"Found issue in this group.\nHEAD: {{head_sha}}\nTEST-GAMING: no\nVERDICT: REQUEST_CHANGES\n"
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
        "chosen_leg": {{"id": "mimo-v2.6-pro", "family": SEAT_FAMILY}},
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

pub fn review_in_repo(
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
