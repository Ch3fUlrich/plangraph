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
    let python = || {
        let mut command = crate::base_fresh::python_command(None);
        command.args([
            "-I",
            "-c",
            r#"
import json, sys
families = {"gpt-6.2-sol": "openai"}
print(json.dumps({text: families.get(text) for text in json.load(sys.stdin)}))
"#,
        ]);
        command
    };
    super::super::cli::run_inner(dir.path(), &args, &python).unwrap();
    let bytes = std::fs::read(&delta).expect("verified delta should be exported");
    let packet: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(packet["artifact"]["base"], m.base);
    assert_eq!(packet["artifact"]["train_sha"], m.train_sha);
    assert_eq!(
        packet["artifact"]["merges"][0]["parents"],
        serde_json::json!([m.base, a])
    );
    assert_eq!(packet["artifact"]["merges"][0]["clean"], true);
    assert_eq!(packet["excluded"], serde_json::json!([]));
    super::super::cli::run_inner(dir.path(), &args, &python).unwrap();
    assert_eq!(std::fs::read(delta).unwrap(), bytes);
}

#[cfg(unix)]
#[test]
fn train_delta_binds_standalone_resolution_to_its_patch_and_writer() {
    let fx = Fx::new();
    let head = fx.commit(&fx.base(), "part.txt", "part\n");
    let later = fx.commit(&fx.base(), "later.txt", "later part\n");
    let parts = vec![part(1, &head, None), part(2, &later, None)];
    let mut manifest = build(&fx.input(parts.clone(), "", None))
        .unwrap()
        .manifest
        .unwrap();
    let merge = manifest.merges[0].clone();
    let fix = fx.commit(&merge, "resolution.txt", "resolved\n");
    g(fx.path(), &["checkout", "--detach", &fix]);
    g(
        fx.path(),
        &[
            "commit",
            "--amend",
            "-m",
            "Train 11: resolve fixture\n\nWriter: gpt-6.2-sol\nHarness: codex",
        ],
    );
    let resolution = g(fx.path(), &["rev-parse", "HEAD"]);
    g(fx.path(), &["merge", "--no-ff", "-m", "later part", &later]);
    manifest.merges[1] = g(fx.path(), &["rev-parse", "HEAD"]);
    g(
        fx.path(),
        &[
            "commit",
            "--allow-empty",
            "-m",
            "Train 11: resolve bookkeeping\n\nWriter: gpt-6.2-sol\nHarness: codex",
        ],
    );
    manifest.train_sha = g(fx.path(), &["rev-parse", "HEAD"]);
    let delta = verify_manifest_with(fx.path(), &manifest, &parts, &[], &[]).unwrap();
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let python = || crate::base_fresh::python_command(Some(&root));
    let packet =
        super::super::delta::ReviewPacket::new(&delta, fx.path(), &manifest, &python).unwrap();
    let packet = serde_json::to_value(packet).unwrap();
    let fix = &packet["artifact"]["resolutions"][0];
    assert_eq!(fix["commit"], resolution);
    assert_eq!(fix["parent"], merge);
    assert_eq!(fix["writer"], "gpt-6.2-sol");
    assert!(fix["patch"].as_str().unwrap().contains("+resolved"));
    assert_eq!(packet["artifact"]["merges"][1]["parents"][0], resolution);
    assert_eq!(
        packet["artifact"]["resolutions"][1]["commit"],
        manifest.train_sha
    );
    assert_eq!(packet["excluded"], serde_json::json!(["openai"]));
    assert!(packet["prompt"].as_str().unwrap().contains("+resolved"));

    // Both a regular merge and a standalone resolution use the pinned replay objects.
    for (commit, parents) in [
        (&merge, vec![manifest.base.as_str(), head.as_str()]),
        (&resolution, vec![merge.as_str()]),
    ] {
        let tree = g(fx.path(), &["rev-parse", &format!("{commit}^{{tree}}")]);
        let mut args = vec!["commit-tree", &tree];
        for parent in parents {
            args.extend(["-p", parent]);
        }
        args.extend(["-m", "replacement\n\nWriter: gemini-4-hydrogen-high"]);
        let replacement = g(fx.path(), &args);
        g(fx.path(), &["replace", commit, &replacement]);
    }
    let delta = verify_manifest_with(fx.path(), &manifest, &parts, &[], &[]).unwrap();
    let with_replacements =
        super::super::delta::ReviewPacket::new(&delta, fx.path(), &manifest, &python).unwrap();
    assert_eq!(serde_json::to_value(with_replacements).unwrap(), packet);
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
        "review_brief_sha256":prompt_hash,
        "witness_kind":"response-side-gateway-call-log",
        "completion":{"head_sha":manifest.train_sha},
        "attempts":[{"status":"completed","served_model":"gemini-4-hydrogen-high",
                     "witness_kind":"response-side-gateway-call-log"}],
        "events":[{"event":"opencode_step_finish","finish_reason":"stop"}],
        "stdout_tail":"VERDICT: PASS\nTEST_GAMING: no\n"
    });
    let file = target.join("record.json");
    let check = |value: &serde_json::Value| {
        pg_worker_core::run::record_io::atomic_write_private(&file, value.to_string().as_bytes())
            .unwrap();
        packet.require_review(&config, "example:train", &id, &python)
    };
    check(&record).unwrap();
    let mut truncated = record.clone();
    truncated["events"].as_array_mut().unwrap().push(json!({
        "event":"opencode_step_finish", "finish_reason":"length"
    }));
    assert!(check(&truncated)
        .unwrap_err()
        .contains("passing witnessed seat"));
    let mut witnessed_finish = record.clone();
    witnessed_finish["attempts"][0]["finish_reason"] = json!("end_turn");
    witnessed_finish["attempts"][0]["stdout_tail"] = record["stdout_tail"].clone();
    witnessed_finish["attempts"]
        .as_array_mut()
        .unwrap()
        .push(json!({
            "status": "skipped", "skip_reason": "earlier_leg_succeeded"
        }));
    check(&witnessed_finish).unwrap();
    witnessed_finish["attempts"][0]["finish_reason"] = json!("length");
    assert!(check(&witnessed_finish)
        .unwrap_err()
        .contains("passing witnessed seat"));
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
fn train_delta_dispatch_routes_a_witnessed_document_review() {
    check_train_delta_dispatch(false);
}

#[cfg(unix)]
#[test]
fn train_delta_dispatch_routes_mimo_when_openai_wrote_the_resolution() {
    check_train_delta_dispatch(true);
}

#[cfg(unix)]
fn check_train_delta_dispatch(openai_resolution: bool) {
    use super::super::delta::ReviewPacket;
    let fx = Fx::new();
    let (parts, resolutions) = if openai_resolution {
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
        let resolution = adopted(&prefix, &p, &g(fx.path(), &["rev-parse", "HEAD"]));
        (vec![part(1, &x, None), part(2, &p, None)], vec![resolution])
    } else {
        let part_head = fx.commit(&fx.base(), "a.txt", "a\n");
        g(fx.path(), &["checkout", "--detach", &part_head]);
        g(
            fx.path(),
            &["commit", "--amend", "-m", "part\n\nWriter: gpt-6.2-sol"],
        );
        let first = g(fx.path(), &["rev-parse", "HEAD"]);
        let other = fx.commit(&fx.base(), "b.txt", "b\n");
        g(fx.path(), &["checkout", "--detach", &other]);
        g(
            fx.path(),
            &[
                "commit",
                "--amend",
                "-m",
                "part\n\nWriter: gemini-4-hydrogen-high",
            ],
        );
        let second = g(fx.path(), &["rev-parse", "HEAD"]);
        (vec![part(1, &first, None), part(2, &second, None)], vec![])
    };
    let mut input = fx.input(parts.clone(), "", None);
    input.resolutions = resolutions.clone();
    let m = build(&input).unwrap().manifest.unwrap();
    let delta = verify_manifest_with(fx.path(), &m, &parts, &resolutions, &[]).unwrap();
    g(fx.path(), &["checkout", "--detach", &m.train_sha]);
    g(
        fx.path(),
        &["remote", "add", "origin", fx.path().to_str().unwrap()],
    );
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let python = || crate::base_fresh::python_command(Some(&root));
    let packet = ReviewPacket::new(&delta, fx.path(), &m, &python).unwrap();
    let state = tempfile::tempdir().unwrap();
    let fake_gateway = || {
        let mut cmd = python();
        cmd.env("PG_DELTA_TEST_STATE", state.path())
            .env(
                "PG_DELTA_TEST_WRITER",
                if openai_resolution { "openai" } else { "" },
            )
            .args([
                "-c",
                r#"
import contextlib, hashlib, io, json, os, sys
from pathlib import Path
from plangraph.spawner import cli, gateway_witness as gw
from plangraph.spawner.runner import spawn_service

args = sys.argv[3:]
card = json.loads(args[args.index('--card') + 1])
writers = [args[i + 1] for i, arg in enumerate(args) if arg == '--writer-family']
prompt = open(args[args.index('--prompt-file') + 1], encoding='utf-8').read()
state = os.environ['PG_DELTA_TEST_STATE']
expected_writer = os.environ['PG_DELTA_TEST_WRITER']
assert writers == ([expected_writer] if expected_writer else []), writers
# Exercise the real overlay and renderer without depending on the host provider file.
os.environ.pop('OPENCODE_CONFIG_CONTENT', None)
providers = Path(state) / 'providers.json'
providers.write_text('{"providers": {}}', encoding='utf-8')
os.environ['PLANGRAPH_PROVIDERS_FILE'] = str(providers)
os.environ['OMNIROUTE_BASE_URL'] = 'http://127.0.0.1:1/v1'
os.environ[gw.TOKEN_ENV] = 'local-fixture'
os.environ['PLANGRAPH_OMNIROUTE_LEG_STATE'] = '0'
os.environ['PLANGRAPH_OWN_HARNESS'] = 'off'
os.environ['XDG_CONFIG_HOME'] = state
os.environ['XDG_STATE_HOME'] = state
plan = spawn_service(state=state)._route(card, 'review', writer_families=writers)
assert gw.witness_gate_legs(card, 'review', plan.legs) == plan.legs
assert 'diff_ref' not in card
rendered = io.StringIO()
with contextlib.redirect_stdout(rendered):
    assert cli.main(['--state', state, *args, '--dry-run']) == 0, rendered.getvalue()
result = json.loads(rendered.getvalue())
assert result['status'] == 'dry_run', (result.get('error'), result.get('attempts'))
assert result['review_document'] == 'train_delta'
attempt = next(a for a in result['attempts'] if a['status'] == 'dry_run')
assert 'finish_reason' in attempt
assert result['review_brief_sha256'] == hashlib.sha256(prompt.encode()).hexdigest()
if expected_writer == 'openai':
    assert result['chosen_leg']['id'] in ('mimo-v2.6-pro', 'mimo-v2.6-flash'), result['chosen_leg']
print(rendered.getvalue())
"#,
            ]);
        cmd
    };
    packet
        .dispatch(fx.path(), "example:train", &fake_gateway)
        .unwrap();
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

#[cfg(unix)]
#[test]
fn go_evidence_binds_the_digest_of_the_manifest_the_proof_parsed() {
    let fx = Fx::new();
    let a = fx.commit(&fx.base(), "a.txt", "a\n");
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
    let manifest = fixture_file(dir.path(), "manifest.json", &m);
    let original = std::fs::read(&manifest).unwrap();
    let args: Vec<String> = [
        "go-evidence",
        "--repo",
        fx.path().to_str().unwrap(),
        "--manifest",
        &manifest,
        "--parts",
        &fixture_file(dir.path(), "parts.json", &parts),
        "--open",
        &fixture_file(dir.path(), "open.json", &open),
        "--freeze",
        &fixture_file(dir.path(), "freeze.json", &freeze),
        "--result",
        &fixture_file(dir.path(), "result.json", &outcome.result),
        "--item",
        "demo:ITEM",
        "--review-run",
        "run-fixture",
        "--pr",
        "9",
        "--out",
        dir.path().join("evidence.json").to_str().unwrap(),
    ]
    .map(String::from)
    .to_vec();
    // The family script runs while the delta is packaged; the review check runs after the proof and finds no
    // stdin: it replaces the manifest file, as a concurrent writer would, and passes.
    let python = || {
        let mut command = crate::base_fresh::python_command(None);
        command.env("SWAP", &manifest).args([
            "-I",
            "-c",
            r#"
import json, os, sys
try:
    texts = json.load(sys.stdin)
except Exception:
    open(os.environ["SWAP"], "w").write('{"replaced": true}')
    sys.exit(0)
print(json.dumps({text: None for text in texts}))
"#,
        ]);
        command
    };
    let seen = std::cell::RefCell::new(None);
    let source = |manifest: &Manifest, digest: &str, run: &str| {
        *seen.borrow_mut() = Some((
            manifest.train_sha.clone(),
            digest.to_owned(),
            run.to_owned(),
        ));
        Err("stop".to_owned())
    };
    let error =
        super::super::cli::run_inner_with(dir.path(), &args, &python, Some(&source)).unwrap_err();
    assert_eq!(error, "stop");
    assert_ne!(
        std::fs::read(&manifest).unwrap(),
        original,
        "the file was replaced after the proof"
    );
    let (train, digest, run) = seen.into_inner().expect("the source was asked");
    assert_eq!(train, m.train_sha);
    assert_eq!(
        digest,
        super::super::go_evidence::manifest_digest(&original)
    );
    assert_eq!(run, "run-fixture");
}
