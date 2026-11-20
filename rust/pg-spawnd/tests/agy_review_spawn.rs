#![cfg(unix)]
#[path = "common/binary.rs"]
mod binary;
mod common;
use common::{ChildGuard, McpClient, TempDir};
use serde_json::{json, Value};
use std::{os::unix::fs::PermissionsExt, path::Path, process::Command};

fn git(dir: &Path, args: &[&str]) -> String {
    let output = pg_worker_core::git_test_support::command(dir)
        .args(args)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8(output.stdout).unwrap().trim().into()
}
fn executable(path: &Path, text: &str) {
    std::fs::write(path, text).unwrap();
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o700)).unwrap();
}
fn payload(reply: &Value) -> Value {
    serde_json::from_str(
        reply["result"]["content"][0]["text"]
            .as_str()
            .unwrap_or_else(|| panic!("{reply}")),
    )
    .unwrap()
}
#[test]
fn review_kind_spawns_a_stamped_repository_review_and_records_it() {
    run_review(false, false, "on");
}
#[test]
fn quota_retry_between_agy_models_keeps_stamped_cli_provenance() {
    run_review(true, false, "on");
}
#[test]
fn quota_retry_from_other_transport_prepares_sonnet_cli_review() {
    run_review(true, true, "on");
}
#[test]
fn stamped_cli_reviews_keep_their_transport_with_inline_rollout_off_or_shadow() {
    for mode in ["off", "shadow"] {
        run_review(false, false, mode);
        run_review(true, false, mode);
        run_review(true, true, mode);
    }
}
fn run_review(retry: bool, sonnet: bool, mode: &str) {
    let state = TempDir::new();
    let repo = state.path().join("repo");
    std::fs::create_dir(&repo).unwrap();
    git(&repo, &["init", "-q"]);
    git(
        &repo,
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
    let head = git(&repo, &["rev-parse", "HEAD"]);
    let leg = json!({"id":"agy-gemini-4-hydrogen-high", "model":"gemini-4-hydrogen-high", "harness":"agy", "family":"gemini"});
    let first = if sonnet {
        json!({"id":"free", "model":"free", "harness":"free", "family":"deepseek"})
    } else {
        leg.clone()
    };
    let leg = if sonnet {
        json!({"id":"sonnet", "model":"claude-sonnet-5-5-high", "harness":"claude", "family":"anthropic"})
    } else if retry {
        json!({"id":"neon", "model":"gemini-4-neon-medium", "harness":"agy", "family":"gemini"})
    } else {
        leg
    };
    let frame = json!({"event":"result", "result":{"status":"SUCCESS", "response":format!("No findings in src/lib.rs:1.\nHEAD: {head}\nVERDICT: PASS\nTEST_GAMING: no\n")}});
    let frame = if sonnet {
        format!(
            "{}\n{}",
            json!({"type":"system", "subtype":"init", "model":"claude-sonnet-5-5-high"}),
            json!({"type":"result", "is_error":false, "result":format!("No findings in src/lib.rs:1.\nHEAD: {head}\nVERDICT: PASS\nTEST_GAMING: no\n")})
        )
    } else {
        frame.to_string()
    };
    let program = state.path().join("reviewer");
    let isolation_checks = if sonnet {
        String::new()
    } else {
        format!("test \"$PWD\" != '{}' || exit 2\ntest \"$(git rev-parse HEAD)\" = '{head}' || exit 3\ntest -z \"$(git remote)\" || exit 4\ncase \"$*\" in *'command(*)'*) ;; *) exit 5;; esac\n", repo.display())
    };
    executable(
        &program,
        &format!("#!/bin/sh\n{isolation_checks}cat >/dev/null\nprintf '%s\\n' '{frame}'\n"),
    );
    let route = state.path().join("route");
    executable(
        &route,
        &format!(
            "#!/bin/sh\nprintf '%s' '{}'\n",
            json!({"first_leg":first, "legs":if retry { vec![first, leg.clone()] } else { vec![leg.clone()] }})
        ),
    );
    let launch = state.path().join("launch");
    let spec = json!({"argv":[program, "--input-format", "stream-json"], "cwd":repo, "env":{}, "stdin":format!("{}\n", json!({"event":"user", "message":{"role":"user", "content":"review"}})), "prompt":"review"});
    let bad = json!({"argv":["/bin/sh", "-c", "echo 'RESOURCE_EXHAUSTED (code 429): Individual quota reached' >&2; exit 1"],
        "cwd":repo, "env":{}, "stdin":"review", "prompt":"review"});
    let quote = |v: &Value| format!("'{}'", v.to_string().replace('\'', "'\\''"));
    let launch_text = if retry {
        format!("#!/bin/sh\nrequest=$(cat)\ncase \"$request\" in *{}*) printf '%s' {};; *) printf '%s' {};; esac\n",
            leg["model"].as_str().unwrap(), quote(&spec), quote(&bad))
    } else {
        format!("#!/bin/sh\ncat >/dev/null\nprintf '%s' {}\n", quote(&spec))
    };
    executable(&launch, &launch_text);
    let socket = state.path().join("socket");
    let child = Command::new(binary::spawnd())
        .args(["serve", "--no-scope", "--socket"])
        .arg(&socket)
        .arg("--state")
        .arg(state.path())
        .env("PG_SPAWND_ADAPTER", &route)
        .env("PG_SPAWND_LAUNCH_CMD", &launch)
        .env("PLANGRAPH_RELIABILITY_LEG_REF", "on")
        .env("PLANGRAPH_RELIABILITY_INLINE_REVIEW_DIFF", mode)
        .spawn()
        .unwrap();
    let _guard = ChildGuard { child };
    let mut client = McpClient::connect(&socket);
    client
        .stream
        .get_mut()
        .set_read_timeout(Some(std::time::Duration::from_secs(45)))
        .unwrap();
    client.initialize();
    let mut request = json!({"name":"plangraph_agent_spawn", "arguments":{
        "role":"review", "writer_families":["openai"], "prompt":"review",
        "card":{"item_key":"demo:AGY", "estimate_tokens":1000, "cwd":repo, "diff_ref":{"repo":repo, "base":"HEAD", "head":"HEAD"}}, "wait_seconds":20
    }});
    if sonnet {
        request["arguments"]["card"]
            .as_object_mut()
            .unwrap()
            .remove("diff_ref");
        request["arguments"]["card"]["head_sha"] = json!(head);
    }
    if !retry {
        request["arguments"]["leg"] = json!("agy-cli");
        let missing_stamp = client.request("tools/call", request.clone());
        assert!(
            missing_stamp["error"]["message"]
                .as_str()
                .is_some_and(|s| s.contains("fast-checks stamp")),
            "{missing_stamp}"
        );
    }
    let stamps = repo.join(".git/fast-checks");
    std::fs::create_dir(&stamps).unwrap();
    std::fs::write(stamps.join(&head), "green").unwrap();
    let reply = client.request("tools/call", request);
    let record = payload(&reply);
    assert_eq!(record["status"], "completed", "{reply}");
    assert_eq!(record["head_sha"], head);
    assert_eq!(record["item_key"], "demo:AGY");
    assert_eq!(record["witness_kind"], "cli_reported");
    assert_eq!(record["verdict"], "pass");
    assert_eq!(record["served_model"], leg["model"]);
    assert_eq!(record["cwd"], json!(repo));
    if !sonnet {
        let clone = Path::new(record["review_checkout"].as_str().unwrap());
        assert_ne!(clone, repo);
        assert!(!clone.exists(), "completed review must clean up its clone");
    }
    let attempts = record["attempts"].as_array().unwrap();
    assert_eq!(attempts.len(), if retry { 2 } else { 1 });
    assert_eq!(attempts.last().unwrap()["witness_kind"], "cli_reported");
    let run_dir = Path::new(record["seat_dir"].as_str().unwrap())
        .parent()
        .unwrap();
    let prompt = std::fs::read_to_string(run_dir.join("prompt.txt")).unwrap();
    assert!(prompt.contains(&format!("Read-only repository review of HEAD {head}")));
    let live_log = std::fs::read_to_string(run_dir.join("live.log")).unwrap();
    let review_txt = std::fs::read_to_string(run_dir.join("seat/review.txt")).unwrap();
    for text in [
        record["stdout_tail"].as_str().unwrap(),
        live_log.as_str(),
        review_txt.as_str(),
    ] {
        assert!(text.contains(&format!("HEAD: {head}")), "{text}");
    }
    assert!(!prompt.contains("<!-- PLANGRAPH INLINE DIFF BEGIN -->"));
    if sonnet {
        assert!(!prompt.contains("INLINE_REVIEW_DIFF"));
    }
    let record_file = state.path().join("finished.json");
    std::fs::write(&record_file, record.to_string()).unwrap();
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let checked = Command::new("python3")
        .current_dir(&root)
        .env("PYTHONPATH", root.join("src"))
        .arg("-c")
        .arg(
            r#"
import json, sys
from pathlib import Path
from plangraph.keeper.evidence_spawner import SpawnerRecordAdapter
record = json.loads(Path(sys.argv[1]).read_text())
root = Path(sys.argv[2]) / 'mirror'
(root / record['host'] / record['run_id']).mkdir(parents=True)
adapter = SpawnerRecordAdapter(root)
adapter.record = lambda *_: record
item = dict(key='demo:AGY', run_ids=json.dumps([record['run_id']]))
assert len(adapter.review_seats(item, record['head_sha'], '', allow_cli_reported=True)) == 1
try:
    adapter.review_seats(item, record['head_sha'], '', allow_cli_reported=False)
except ValueError:
    pass
else:
    raise AssertionError('low-trust seat admitted without rule 3.26')
"#,
        )
        .arg(record_file)
        .arg(state.path())
        .output()
        .unwrap();
    assert!(
        checked.status.success(),
        "{}",
        String::from_utf8_lossy(&checked.stderr)
    );
}
