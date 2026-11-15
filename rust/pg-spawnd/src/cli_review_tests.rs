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
    // Keeper imports fcntl and is POSIX-only; record/provenance assertions run everywhere.
    #[cfg(unix)]
    assert_keeper_accepts_cli_review(dir.path(), record.path(), &head, &id);
}

#[cfg(unix)]
fn assert_keeper_accepts_cli_review(dir: &Path, record: &Path, head: &str, id: &str) {
    let mirror = dir.join("mirror/fixture").join(id);
    std::fs::create_dir_all(&mirror).unwrap();
    {
        use std::os::unix::fs::PermissionsExt;
        for path in [mirror.clone(), mirror.parent().unwrap().into()] {
            std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o700)).unwrap();
        }
    }
    std::fs::copy(record, mirror.join("record.json")).unwrap();
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let output = Command::new("python3")
        .current_dir(&root)
        .env("PYTHONPATH", root.join("src"))
        .args([
            "-c",
            r#"
import json, sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from plangraph.keeper import auth, db, context_sources
from plangraph.keeper.store import Store
from plangraph.keeper.evidence import DecisionVerifier
from plangraph.keeper.evidence_types import Result
from plangraph.keeper.evidence_decisions import DecisionsAdapter
from plangraph.keeper.evidence_spawner import SpawnerRecordAdapter
from plangraph.keeper.errors import Refused
root = Path(sys.argv[1]); head = sys.argv[2]; merge = '1' * 40
class MergedGit:
    def repo_path(self, project):
        assert project == 'demo'
        return 'fixture-repo'
    def repo_ref(self, project):
        return 'main'
    def merge_commit_of(self, repo, sha, ref):
        return (merge, 'fixture@example.invalid') if sha == head else None
    def _run(self, repo, args):
        assert args == ['show', '-s', '--format=%P', '--end-of-options', merge]
        return SimpleNamespace(returncode=0, stdout=f"{'2' * 40} {head}")
conn = db.open_db(root / 'keeper.db', create=True, backend='sqlite')
router = auth.authenticate(conn, auth.add_principal(conn, 'router', 'router', []))
writer = auth.authenticate(conn, auth.add_principal(conn, 'writer', 'l1', ['demo'], family='teamA'))
store = Store(conn, clock=lambda: 1700000000.0,
              verifiers={'decision': SimpleNamespace(verify=lambda *a: Result(True, 'verified'))})
store.create_items(router, [dict(
    project='demo', id=name, title=name, kind=kind, track='T0', owner='tester',
    estimate_h=1, depends_on=deps, done_criteria='criteria', evidence_kinds=['any'],
    operator=False, review_tier='standard'
) for name, kind, deps in [('A', 'lane', []), ('G', 'goal', ['demo:A'])]])
epoch = store.claim(writer, 'demo:A')['epoch']
store.add_evidence(writer, 'demo:A', epoch, 'decision', {'id':'D-1'})
store.submit(writer, 'demo:A', epoch)
decision_path = root / 'DECISIONS.md'
decision_path.write_text(f'| D-5504 | demo | GO {head}; Review-Runs: {sys.argv[3]} |\n')
store.ctx = {'decisions': DecisionsAdapter(decision_path), 'git': MergedGit(),
             'spawner_records': SpawnerRecordAdapter(root / 'mirror')}
store.verifiers['decision'] = DecisionVerifier()
with patch.object(context_sources, 'plan_item_commits', return_value=[
    {'sha':head, 'date':'2026-01-01', 'merge':{'pr':1}}
]):
    for risk in ['auth', 'secrets']:
        store.put_card(router, 'demo:A', dict(outcome='fixture', risk_tags=[risk]))
        try:
            store.close_merged(router, 'demo:A', 'D-5504')
        except Refused as exc:
            assert 'NO-WITNESS' in exc.msg, exc.msg
        else:
            raise AssertionError('sensitive CLI review was accepted')
    store.put_card(router, 'demo:A', dict(outcome='fixture', risk_tags=[]))
    result = store.close_merged(router, 'demo:A', 'D-5504')
    print(json.dumps(result))
conn.close()
"#,
        ])
        .arg(dir)
        .arg(head)
        .arg(id)
        .output()
        .unwrap();
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

#[test]
fn stderr_quota_survives_missing_log_and_sonnet_uses_the_model_store() {
    let dir = tempfile::tempdir().unwrap();
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    for (harness, model) in [
        ("agy", "gemini-4-hydrogen-high"),
        ("claude", "claude-sonnet-5-5-high"),
    ] {
        let id = new_run_id();
        let record = crate::runs::create(dir.path(), &id, "fixture", json!({
            "role":"review", "stderr_tail":"RESOURCE_EXHAUSTED (code 429): Individual quota reached. Resets in 5h."
        }), Status::Failed).ok().unwrap();
        assert!(!record.path().with_file_name("live.log").exists());
        let argv = quota_args(&record, &json!({"harness":harness,"model":model})).unwrap();
        let output = Command::new("python3")
            .current_dir(&root)
            .env("PYTHONPATH", root.join("src"))
            .env("XDG_STATE_HOME", dir.path())
            .args(["-m", "plangraph.spawner.cli", "--state"])
            .arg(dir.path())
            .args(argv)
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
    }
    let output = Command::new("python3").current_dir(&root)
        .env("PYTHONPATH", root.join("src")).env("XDG_STATE_HOME", dir.path())
        .args(["-c", "import json; from plangraph.routing_support.leg_state import _default_store; print(json.dumps({m:_default_store().leg_status('agy:'+m) for m in ['gemini-4-hydrogen','gemini-4-argon','claude-sonnet-5-5']}))"])
        .output().unwrap();
    assert!(output.status.success());
    let states: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(states["gemini-4-hydrogen"]["state"], "limited");
    assert_eq!(states["claude-sonnet-5-5"]["state"], "limited");
    assert_eq!(states["gemini-4-argon"]["state"], "ok");
    let id = new_run_id();
    let record = crate::runs::create(
        dir.path(),
        &id,
        "fixture",
        json!({"role":"review"}),
        Status::Failed,
    )
    .ok()
    .unwrap();
    std::fs::write(record.path().with_file_name("live.log"), format!("{}\n", json!({"type":"rate_limit_event", "rate_limit_info":{"status":"rejected", "resetsAt":2000000030}}))).unwrap();
    let argv = quota_args(
        &record,
        &json!({"harness":"claude", "model":"claude-sonnet-5-5-high"}),
    )
    .unwrap();
    assert_eq!(&argv[argv.len() - 2..], ["--reset-at", "2000000030"]);
}

#[test]
fn real_sonnet_launch_adapter_keeps_the_selected_harness_and_python_rejects_kind() {
    let dir = tempfile::tempdir().unwrap();
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    // CI may have no fetchable default branch in its detached source checkout.
    // Give the fixture its own local origin, pinned to the revision under test.
    let origin = dir.path().join("origin.git");
    assert!(Command::new("git")
        .args(["clone", "-q", "--bare", "--shared"])
        .arg(&root)
        .arg(&origin)
        .status()
        .unwrap()
        .success());
    git_ok(&origin, &["update-ref", "refs/heads/main", "HEAD"]);
    git_ok(&origin, &["symbolic-ref", "HEAD", "refs/heads/main"]);
    let checkout = dir.path().join("checkout");
    assert!(Command::new("git")
        .args(["clone", "-q", "--shared"])
        .arg(&origin)
        .arg(&checkout)
        .status()
        .unwrap()
        .success());
    let output = Command::new("python3").current_dir(&checkout)
        .env("PYTHONPATH", root.join("src")).env("XDG_STATE_HOME", dir.path())
        .env("PLANGRAPH_OMNIROUTE_LEG_STATE", "0")
        .args(["-c", r#"
import asyncio, json, sys
from pathlib import Path
from plangraph.spawner.launch_spec import build
from plangraph.spawner.service import SpawnService
from plangraph.spawner.mcp import MCPSpawnerServer, ParameterRefused, TOOLS_METADATA
state = str(Path(sys.argv[1]).resolve())
request = dict(run_id='run-'+'a'*32, job_id='fixture', role='review', dry_run=True,
    leg=dict(id='claude-sonnet-5-5-high'), prompt='review', writer_families=['openai'], card=dict(estimate_tokens=1000), cwd=str(Path.cwd()), state=state)
argv = build(request)['argv']
assert argv[0] == 'claude', argv
assert argv[argv.index('--model')+1] == 'claude-sonnet-5-5', argv
assert argv[argv.index('--effort')+1] == 'high', argv
record = asyncio.run(SpawnService(state=state).spawn(card=dict(estimate_tokens=1000),
    prompt='review', role='review', leg='claude-sonnet-5-5-high', writer_families=['openai'],
    override_reason='fixture direct native Sonnet', dry_run=True))
assert record['status'] == 'dry_run', record
assert record['chosen_leg']['harness'] == 'claude', record
server = MCPSpawnerServer(state=state)
for verb in ['route', 'spawn']:
    if verb == 'route':
        assert 'leg' not in next(t for t in TOOLS_METADATA if t['name']=='plangraph_agent_'+verb)['inputSchema']['properties']
    try:
        asyncio.run(server.call_tool('plangraph_agent_'+verb, dict(leg='agy-cli')))
    except ParameterRefused as exc:
        assert 'pg-spawnd' in str(exc)
    else:
        raise AssertionError('Python MCP silently accepted a Rust selector')
print(json.dumps(dict(argv=argv, status=record['status'])))
"#]).arg(dir.path()).output().unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let value: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(value["argv"][0], "claude");
    assert_eq!(value["status"], "dry_run");
}
