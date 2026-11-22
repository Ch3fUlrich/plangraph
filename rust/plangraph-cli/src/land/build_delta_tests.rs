use super::*;
use serde::Serialize;

fn fixture_file(dir: &Path, name: &str, value: &impl Serialize) -> String {
    let path = dir.join(name);
    std::fs::write(&path, serde_json::to_vec(value).unwrap()).unwrap();
    path.to_str().unwrap().to_owned()
}

#[test]
fn train_verify_exports_replay_delta_for_review() {
    let fx = Fx::new();
    let a = fx.commit(&fx.base(), "a.txt", "a\n");
    g(fx.path(), &["checkout", "--detach", &a]);
    g(
        fx.path(),
        &["commit", "--amend", "-m", "part\n\nWriter: gpt-6.2-sol"],
    );
    let a = g(fx.path(), &["rev-parse", "HEAD"]);
    let parts = vec![part(1, &a, None)];
    let outcome = build(&fx.input(parts.clone(), "", None)).unwrap();
    let m = outcome.manifest.unwrap();
    let open = BuildOpen {
        build: m.build.clone(),
        base: m.base.clone(),
        policy_version: m.policy.version.clone(),
        grant_digest: "fixture".into(),
        snapshot_position: 1,
    };
    let freeze = Freeze {
        policy_version: open.policy_version.clone(),
        base: open.base.clone(),
        grant_digest: open.grant_digest.clone(),
        selection: parts.clone(),
    };
    let dir = tempfile::tempdir().unwrap();
    let delta = dir.path().join("delta.json");
    let args = vec![
        "verify".into(),
        "--repo".into(),
        fx.path().to_str().unwrap().into(),
        "--manifest".into(),
        fixture_file(dir.path(), "manifest.json", &m),
        "--parts".into(),
        fixture_file(dir.path(), "parts.json", &parts),
        "--open".into(),
        fixture_file(dir.path(), "open.json", &open),
        "--freeze".into(),
        fixture_file(dir.path(), "freeze.json", &freeze),
        "--result".into(),
        fixture_file(dir.path(), "result.json", &outcome.result),
        "--delta-out".into(),
        delta.to_str().unwrap().into(),
    ];
    assert_eq!(super::super::cli::run(dir.path(), &args), 0);
    let bytes = std::fs::read(&delta).expect("verified delta should be exported");
    let packet: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(packet["artifact"]["base"], m.base);
    assert_eq!(packet["artifact"]["train_sha"], m.train_sha);
    assert_eq!(
        packet["artifact"]["merges"][0]["parents"],
        serde_json::json!([m.base, a])
    );
    assert_eq!(packet["artifact"]["merges"][0]["clean"], true);
    assert_eq!(super::super::cli::run(dir.path(), &args), 0);
    assert_eq!(std::fs::read(delta).unwrap(), bytes);
}

#[cfg(unix)]
#[test]
fn train_delta_admission_requires_an_independent_authenticated_seat_on_its_prompt() {
    use super::super::delta::ReviewPacket;
    use serde_json::json;
    use sha2::{Digest, Sha256};
    use std::os::unix::fs::PermissionsExt;

    let fx = Fx::new();
    let (x, p, prefix) = conflicting(&fx);
    let candidate = resolve(&fx, &prefix, &p, "shared.txt", "final resolution\n");
    g(fx.path(), &["checkout", "--detach", &candidate]);
    g(
        fx.path(),
        &[
            "commit",
            "--amend",
            "-m",
            "resolution\n\nWriter: gpt-6.2-sol",
        ],
    );
    let candidate = g(fx.path(), &["rev-parse", "HEAD"]);
    let resolution = adopted(&prefix, &p, &candidate);
    let parts = vec![part(1, &x, None), part(2, &p, None)];
    let mut input = fx.input(parts.clone(), "", None);
    input.resolutions.push(resolution.clone());
    let manifest = build(&input).unwrap().manifest.unwrap();
    let delta = verify_manifest_with(fx.path(), &manifest, &parts, &[resolution], &[]).unwrap();
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let python = || crate::base_fresh::python_command(Some(&root));
    let packet = ReviewPacket::new(&delta, fx.path(), &manifest, &python).unwrap();
    let rendered = serde_json::to_value(&packet).unwrap();
    assert_eq!(rendered["excluded"], json!(["openai"]));
    assert!(rendered["artifact"]["merges"][1]["remerge_diff"]
        .as_str()
        .unwrap()
        .contains("+final resolution"));
    let prompt_hash = format!("{:x}", Sha256::digest(rendered["prompt"].as_str().unwrap()));

    let dir = tempfile::tempdir().unwrap();
    let mirror = dir.path().join("mirror");
    let id = format!("run-{}", "a".repeat(32));
    let target = mirror.join("fixture").join(&id);
    std::fs::create_dir_all(&target).unwrap();
    for path in [&mirror, &mirror.join("fixture"), &target] {
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o700)).unwrap();
    }
    let config = dir.path().join("keeper.json");
    std::fs::write(&config, json!({"spawner_records_dir": mirror}).to_string()).unwrap();
    std::fs::set_permissions(&config, std::fs::Permissions::from_mode(0o600)).unwrap();
    let mut record = json!({
        "run_id":id, "host":"fixture", "role":"review", "item_key":"example:train",
        "status":"completed", "exit_code":0, "served_model":"gemini-4-hydrogen-high",
        "served_family":"google", "prompt_sha256":"d".repeat(64),
        "review_brief_sha256":prompt_hash, "finish_reason":"stop",
        "witness_kind":"response-side-gateway-call-log",
        "completion":{"head_sha":manifest.train_sha},
        "attempts":[{"status":"completed","served_model":"gemini-4-hydrogen-high",
                     "witness_kind":"response-side-gateway-call-log"}],
        "stdout_tail":"VERDICT: PASS\nTEST_GAMING: no\n"
    });
    let file = target.join("record.json");
    let check = |value: &serde_json::Value| {
        pg_worker_core::run::record_io::atomic_write_private(&file, value.to_string().as_bytes())
            .unwrap();
        packet.require_review(&config, "example:train", &id, &python)
    };
    check(&record).unwrap();
    let mut wrong = record.clone();
    wrong["completion"]["head_sha"] = json!(manifest.base);
    assert!(check(&wrong)
        .unwrap_err()
        .contains("passing witnessed seat"));
    wrong = record.clone();
    wrong["review_brief_sha256"] = json!("0".repeat(64));
    assert!(check(&wrong)
        .unwrap_err()
        .contains("prompt binding mismatch"));
    wrong = record.clone();
    wrong["attempts"][0]["witness_kind"] = json!("cli_reported");
    assert!(check(&wrong)
        .unwrap_err()
        .contains("authenticated serving attempt"));
    record["served_model"] = json!("gpt-6.2-sol");
    record["served_family"] = json!("openai");
    record["attempts"][0]["served_model"] = record["served_model"].clone();
    assert!(check(&record)
        .unwrap_err()
        .contains("reviewer family wrote"));
}

#[cfg(unix)]
#[test]
fn train_delta_runner_binds_the_review_brief_inside_the_prepared_prompt() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let out = crate::base_fresh::python_command(Some(&root)).args(["-c", r#"
import asyncio, hashlib, tempfile
from pathlib import Path
from unittest.mock import patch
from plangraph.spawner.runner import SpawnerRunner

brief = 'Review the complete train replay artifact.\n{"clean":true}'
with tempfile.TemporaryDirectory() as cwd:
    record = {}
    path = Path(cwd) / 'prompt.txt'
    with patch('plangraph.spawner.target.resolve_target', return_value=('fixture/model', None)), \
         patch('plangraph.planner.routing.validate_model'), \
         patch('plangraph.spawner.runner.build_command', side_effect=RuntimeError('fixture: stop before launch')):
        try:
            asyncio.run(SpawnerRunner(cwd).spawn(brief, 'fixture/model', role='review',
                        prompt_record=(path, record, lambda record: None)))
        except RuntimeError as exc:
            assert str(exc) == 'fixture: stop before launch'
    rendered = path.read_text()
    assert rendered != brief and rendered.endswith(brief)
    assert record['prompt_sha256'] == hashlib.sha256(rendered.encode()).hexdigest()
    assert record['review_brief_sha256'] == hashlib.sha256(brief.encode()).hexdigest()
"#]).output().unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
}
