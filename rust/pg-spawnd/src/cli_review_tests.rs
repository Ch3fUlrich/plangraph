use super::*;
use pg_worker_core::run::record::new_run_id;
use std::process::Command;

fn git_ok(dir: &Path, args: &[&str]) {
    assert!(Command::new("git")
        .current_dir(dir)
        .args(args)
        .status()
        .unwrap()
        .success());
}
#[test]
fn precondition_binds_exact_stamped_head_and_rejects_dirty_tree() {
    let dir = tempfile::tempdir().unwrap();
    git_ok(dir.path(), &["init", "-q"]);
    git_ok(
        dir.path(),
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
    let mut launch = LaunchSpec {
        argv: vec!["agy".into()],
        cwd: dir.path().into(),
        env: Default::default(),
        stdin: Some("review".into()),
        prompt: Some("review".into()),
        warnings: None,
        events: None,
    };
    let args = json!({"card":{"diff_ref":{"head":"HEAD"}, "cwd":dir.path()}});
    assert_eq!(
        cwd(args.as_object().unwrap(), std::env::temp_dir())
            .ok()
            .unwrap(),
        dir.path()
    );
    assert!(prepare(
        args.as_object().unwrap(),
        &mut launch,
        &json!({"harness":"agy"})
    )
    .is_err());
    let head = git(dir.path(), &["rev-parse", "HEAD"]).ok().unwrap();
    let stamps = dir.path().join(".git/fast-checks");
    std::fs::create_dir(&stamps).unwrap();
    std::fs::write(stamps.join(&head), "green").unwrap();
    assert_eq!(
        prepare(
            args.as_object().unwrap(),
            &mut launch,
            &json!({"harness":"agy"})
        )
        .ok()
        .unwrap(),
        Some(head)
    );
    assert!(launch.argv.iter().any(|s| s == "--settings"));
    assert!(!launch.argv.iter().any(|s| s.contains("command(*)")));
    let wrong = json!({"card":{"head_sha":"wrong"}});
    assert!(prepare(
        wrong.as_object().unwrap(),
        &mut launch,
        &json!({"harness":"agy"})
    )
    .is_err());
    std::fs::write(dir.path().join("tracked"), "x").unwrap();
    git_ok(dir.path(), &["add", "tracked"]);
    assert!(prepare(
        args.as_object().unwrap(),
        &mut launch,
        &json!({"harness":"agy"})
    )
    .is_err());
}
#[test]
fn successful_agy_run_records_head_verdict_findings_and_cli_provenance() {
    let dir = tempfile::tempdir().unwrap();
    git_ok(dir.path(), &["init", "-q"]);
    git_ok(
        dir.path(),
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
    let head = git(dir.path(), &["rev-parse", "HEAD"]).ok().unwrap();
    let id = new_run_id();
    let leg = json!({"id":"agy-gemini-4-hydrogen-high", "harness":"agy", "model":"gemini-4-hydrogen-high", "family":"gemini"});
    let mut record = crate::runs::create(
        dir.path(),
        &id,
        "fixture",
        json!({
            "role":"review", "head_sha":head, "cwd":dir.path(), "item_key":"demo:A", "exit_code":0,
            "attempts":[{"leg":leg,"status":"running"}]
        }),
        Status::Completed,
    )
    .ok()
    .unwrap();
    let log = record.path().with_file_name("live.log");
    let answer = "No findings.\nVERDICT: PASS\nTEST_GAMING: no\n";
    std::fs::write(
        &log,
        format!(
            "{}\n",
            json!({"event":"result", "result":{"status":"SUCCESS", "response":answer}})
        ),
    )
    .unwrap();
    record.set("live_log", json!(log));
    crate::runs::finish(&mut record, Status::Completed, &leg, 1);
    let value = record.to_value();
    assert_eq!(value["served_model"], "gemini-4-hydrogen-high");
    assert_eq!(value["served_family"], "google");
    assert_eq!(value["witness_kind"], "cli_reported");
    assert_eq!(value["completion"]["head_sha"], head);
    assert_eq!(value["verdict"], "pass");
    assert_eq!(value["findings"], "No findings.");
    assert_eq!(value["attempts"][0]["witness_kind"], "cli_reported");
    record.save().unwrap();
    let mirror = dir.path().join("mirror/fixture").join(&id);
    std::fs::create_dir_all(&mirror).unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        for path in [mirror.clone(), mirror.parent().unwrap().into()] {
            std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o700)).unwrap();
        }
    }
    std::fs::copy(record.path(), mirror.join("record.json")).unwrap();
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let output = Command::new("python3").current_dir(&root)
        .env("PYTHONPATH", format!("{}:{}", root.join("src").display(), root.join("tests").display()))
        .args(["-c", r#"
import json, sys
from pathlib import Path
import pytest
import test_keeper_close_merged as fixture
from plangraph.keeper.evidence_spawner import SpawnerRecordAdapter
from plangraph.keeper.errors import Refused
fixture.HEAD = sys.argv[2]
root = Path(sys.argv[1]); work = root / 'keeper'; work.mkdir()
with pytest.MonkeyPatch.context() as patch:
    gen = fixture.unseated_w.__wrapped__(work, patch)
    world = next(gen)
    world.store.ctx['spawner_records'] = SpawnerRecordAdapter(root / 'mirror')
    world.decision_path.write_text(f'| D-5504 | demo | GO {fixture.HEAD}; Review-Runs: {sys.argv[3]} |\n')
    for risk in ['auth', 'secrets']:
        world.store.put_card(world.p('router'), 'demo:A', dict(outcome='fixture', risk_tags=[risk]))
        try:
            world.store.close_merged(world.p('router'), 'demo:A', 'D-5504')
        except Refused as exc:
            assert 'NO-WITNESS' in exc.msg, exc.msg
        else:
            raise AssertionError('sensitive CLI review was accepted')
    world.store.put_card(world.p('router'), 'demo:A', dict(outcome='fixture', risk_tags=[]))
    result = world.store.close_merged(world.p('router'), 'demo:A', 'D-5504')
    print(json.dumps(result))
    try: next(gen)
    except StopIteration: pass
"#]).arg(dir.path()).arg(&head).arg(&id).output().unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let closed: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(closed["state"], "done");
}
#[test]
fn cli_kind_cannot_select_a_skipped_model() {
    let args = json!({"role":"review", "leg":"agy-cli"});
    let route = json!({"legs":[{"harness":"opencode", "model":"opencode-go/beam-free"}]});
    assert!(select(args.as_object().unwrap(), route.to_string()).is_err());
}
