use crate::launch::*;
use serde_json::{json, Value};
use std::time::{Duration, Instant};

struct TestDir(std::path::PathBuf);
impl std::ops::Deref for TestDir {
    type Target = std::path::Path;
    fn deref(&self) -> &Self::Target {
        &self.0
    }
}
impl Drop for TestDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}
fn fixture() -> (TestDir, Value) {
    let dir = std::env::temp_dir().join(pg_worker_core::run::record::new_run_id());
    std::fs::create_dir(&dir).unwrap();
    let spec = json!({"argv":["sh"],"cwd":dir,"env":{},"stdin":null});
    (TestDir(dir), spec)
}
fn script(dir: &std::path::Path, body: &str) -> LaunchAdapter {
    let path = dir.join("launch");
    pg_worker_core::executable_test_support::write_private_executable(
        &path,
        format!("#!/bin/sh\n{body}\n"),
    );
    LaunchAdapter(Some(path.into_os_string()), "python3".into())
}
#[test]
fn contract_table() {
    let (dir, valid) = fixture();
    for (key, value) in [
        ("argv", json!([])),
        ("argv", json!([1])),
        ("argv", json!(["-sh"])),
        ("argv", json!([""])),
        ("argv", json!(["sh\0"])),
        ("argv", json!(vec!["sh"; 1025])),
        ("cwd", json!(dir.join("missing"))),
        ("cwd", json!(1)),
        ("env", json!({"X":1})),
        ("env", json!({"X=Y":"v"})),
        ("env", json!({"":"v"})),
        ("env", json!({"X\0":"v"})),
        ("env", json!({"X":"v\0"})),
        ("stdin", json!(1)),
        ("stdin", json!("x".repeat(8 * 1024 * 1024 + 1))),
        ("warnings", json!([1])),
        ("events", json!([{}])),
        ("events", json!([{"event":1}])),
    ] {
        let mut value_spec = valid.clone();
        value_spec[key] = value;
        assert!(
            matches!(parse(&serde_json::to_vec(&value_spec).unwrap()), Err(crate::errors::ToolError::Internal(s)) if s == "launch adapter returned an invalid launch spec"),
            "{key}"
        );
    }
    let mut missing = valid.clone();
    let mut large_env = valid.clone();
    large_env["env"] = Value::Object(
        (0..1025)
            .map(|n| (format!("KEY{n}"), json!("value")))
            .collect(),
    );
    assert!(parse(&serde_json::to_vec(&large_env).unwrap()).is_err());
    missing.as_object_mut().unwrap().remove("stdin");
    assert!(parse(&serde_json::to_vec(&missing).unwrap()).is_err());
    let mut good = valid;
    good["warnings"] = json!(["warning"]);
    good["events"] = json!([{"event":"ready","extra":true}]);
    good["unknown"] = json!(true);
    let spec = parse(&serde_json::to_vec(&good).unwrap()).ok().unwrap();
    assert_eq!(spec.warnings.unwrap(), ["warning"]);
    assert_eq!(spec.events.unwrap()[0]["event"], "ready");
}
#[test]
fn request_pipe_and_exits() {
    let (dir, valid) = fixture();
    std::fs::write(dir.join("spec"), serde_json::to_vec(&valid).unwrap()).unwrap();
    let adapter = script(
        &dir,
        &format!("cat > '{0}/request'\ncat '{0}/spec'", dir.display()),
    );
    let request = json!({"run_id":"run","job_id":"job","role":"implement","dry_run":false,"leg":{},"prompt":" verbatim\n","card":{},"cwd":dir.0,"state":dir.0});
    assert!(adapter
        .request(&dir, &request, Duration::from_secs(1))
        .is_ok());
    let saved: Value =
        serde_json::from_slice(&std::fs::read(dir.join("request")).unwrap()).unwrap();
    assert_eq!(saved, request);
    assert_eq!(saved.as_object().unwrap().len(), 9);
    for (body, expected) in [
        (
            "echo 'SpawnRefused: nope' >&2; exit 2",
            "SpawnRefused: nope",
        ),
        ("exit 1", "launch adapter failed (exit 1)"),
        ("kill -9 $$", "launch adapter killed by a signal"),
        (
            "echo broken",
            "launch adapter returned an invalid launch spec",
        ),
    ] {
        let error = script(&dir, body)
            .request(&dir, &request, Duration::from_secs(1))
            .err()
            .unwrap()
            .into_error_data();
        assert_eq!(error.message, expected);
    }
}
#[test]
fn timeout_kills_group() {
    let (dir, _) = fixture();
    let adapter = script(
        &dir,
        &format!("echo $$ > '{}/pid'\nsleep 30 &\nwait", dir.display()),
    );
    // A loaded host can kill the script before its first line wrote the pid; such a
    // round proves nothing about the group, so it repeats with a longer timeout.
    for timeout in [300, 1_000, 3_000, 10_000].map(Duration::from_millis) {
        let _ = std::fs::remove_file(dir.join("pid"));
        let start = Instant::now();
        let error = adapter
            .request(&dir, &json!({}), timeout)
            .err()
            .unwrap()
            .into_error_data();
        assert_eq!(error.message, "launch adapter timed out");
        // Well under the 30 s sleep: the group was killed, not waited for.
        assert!(start.elapsed() < timeout + Duration::from_secs(10));
        let Some(pid) = std::fs::read_to_string(dir.join("pid"))
            .ok()
            .and_then(|text| text.trim().parse::<i32>().ok())
        else {
            continue;
        };
        assert_eq!(unsafe { libc::kill(pid, 0) }, -1);
        return;
    }
    panic!("the launch adapter script never started");
}

#[test]
fn exited_adapter_closes_descendant_pipes() {
    let (dir, valid) = fixture();
    std::fs::write(dir.join("spec"), serde_json::to_vec(&valid).unwrap()).unwrap();
    let adapter = script(&dir, &format!("cat '{}/spec'\n(sleep 30 &)", dir.display()));
    let start = Instant::now();
    // Waiting for the descendant's pipe would end in the 10 s timeout (an error).
    assert!(adapter
        .request(&dir, &json!({}), Duration::from_secs(10))
        .is_ok());
    assert!(start.elapsed() < Duration::from_secs(10));
}

#[test]
fn lookup_rules_and_missing_program() {
    let (dir, _) = fixture();
    let default = LaunchAdapter::from_lookup(&|_| None);
    assert!(default.0.is_none());
    assert_eq!(default.1, "python3");
    let adapter = LaunchAdapter::from_lookup(&|key| match key {
        "PG_SPAWND_LAUNCH_CMD" => Some("".into()),
        "PG_SPAWND_PYTHON" => Some(dir.join("missing").into_os_string()),
        _ => None,
    });
    assert!(adapter.0.is_none());
    assert_eq!(adapter.1, dir.join("missing").into_os_string());
    let relative = LaunchAdapter::from_lookup(&|_| Some("relative".into()));
    for (adapter, has_os_error) in [(adapter, true), (relative, false)] {
        let message = adapter
            .request(&dir, &json!({}), Duration::from_secs(1))
            .err()
            .unwrap()
            .into_error_data()
            .message;
        assert!(
            message.starts_with("launch adapter could not start"),
            "{message}"
        );
        assert_eq!(message.contains("os error"), has_os_error, "{message}");
    }
}

#[test]
fn refusal_redaction_truncation_and_empty_line() {
    let (dir, _) = fixture();
    let secret = ["sk-", &"x".repeat(40)].concat();
    let line = format!("{secret} {}", "z ".repeat(200));
    let expected: String = pg_worker_core::redact::redact(&line)
        .chars()
        .take(300)
        .collect();
    assert!(!expected.contains(&secret));
    assert_eq!(expected.chars().count(), 300);
    for (line, expected) in [
        (line.as_str(), expected.as_str()),
        ("\nignored", "launch refused"),
    ] {
        let adapter = script(&dir, &format!("printf '%s\\n' '{line}' >&2; exit 2"));
        assert_eq!(
            adapter
                .request(&dir, &json!({}), Duration::from_secs(1))
                .err()
                .unwrap()
                .into_error_data()
                .message,
            expected
        );
    }
}

fn ok(result: Result<Value, crate::errors::ToolError>) -> Value {
    match result {
        Ok(value) => value,
        Err(_) => panic!("with_head_writers refused"),
    }
}

fn repo_with_writer_trailer(trailer: Option<&str>) -> (TestDir, crate::launch::RepoMap, Value) {
    use pg_worker_core::git_test_support::command as git;
    let dir = std::env::temp_dir().join(pg_worker_core::run::record::new_run_id());
    std::fs::create_dir(&dir).unwrap();
    let run = |args: &[&str]| {
        let out = git(&dir).args(args).output().unwrap();
        assert!(
            out.status.success(),
            "{}",
            String::from_utf8_lossy(&out.stderr)
        );
        String::from_utf8_lossy(&out.stdout).trim().to_owned()
    };
    run(&["init", "-q"]);
    run(&["config", "user.name", "Fixture"]);
    run(&["config", "user.email", "fixture@example.invalid"]);
    std::fs::write(dir.join("f"), "a\n").unwrap();
    run(&["add", "f"]);
    run(&["commit", "-qm", "base"]);
    let base = run(&["rev-parse", "HEAD"]);
    std::fs::write(dir.join("f"), "b\n").unwrap();
    let message = match trailer {
        Some(t) => format!("change\n\nWriter: {t}"),
        None => "change".to_owned(),
    };
    run(&["commit", "-qam", &message]);
    let head = run(&["rev-parse", "HEAD"]);
    let entry = pg_worker_core::hosts::RepoEntry {
        root: dir.to_string_lossy().into(),
        worktrees_root: dir.join("trees").to_string_lossy().into(),
        remote: String::new(),
        python: None,
        import_roots: vec![],
        cache_env: Default::default(),
        live_dir: None,
        health_checks: None,
    };
    let repos = [("demo".to_owned(), entry)].into_iter().collect();
    let request = json!({"role":"review","writer_families":["openai"],
        "card":{"diff_ref":{"repo_id":"demo","base_sha":base,"head_sha":head}}});
    (TestDir(dir), repos, request)
}

#[test]
fn a_registered_repository_review_adds_the_head_writer_trailer_families() {
    let (_dir, repos, request) =
        repo_with_writer_trailer(Some("gemini-4-hydrogen-high run-1 (writer)"));
    let out = ok(with_head_writers(&request, &repos));
    assert_eq!(
        out["writer_families"],
        json!(["openai", "gemini-4-hydrogen-high"])
    );
    // Idempotent: a family already named is not repeated.
    assert_eq!(ok(with_head_writers(&out, &repos)), out);
}

#[test]
fn a_head_without_trailer_or_a_card_without_registered_ref_is_unchanged() {
    let (_dir, repos, request) = repo_with_writer_trailer(None);
    assert_eq!(ok(with_head_writers(&request, &repos)), request);
    let plain = json!({"role":"review","writer_families":["openai"],
        "card":{"diff_ref":{"repo":"/x","base":"a","head":"b"}}});
    assert_eq!(ok(with_head_writers(&plain, &repos)), plain);
    let implement = json!({"role":"implement","card":{}});
    assert_eq!(ok(with_head_writers(&implement, &repos)), implement);
}

#[test]
fn an_unreadable_registered_head_refuses_instead_of_assuming_no_writer() {
    let (_dir, repos, mut request) =
        repo_with_writer_trailer(Some("gemini-4-hydrogen-high run-1 (writer)"));
    request["card"]["diff_ref"]["head_sha"] = json!("0".repeat(40));
    let error = with_head_writers(&request, &repos).unwrap_err();
    assert!(
        matches!(&error, crate::errors::ToolError::Refused { message, .. } if message.contains("cannot read the Writer trailer")),
        "refused with another error"
    );
}

#[test]
fn registered_head_fetch_timeout_reaps_git_and_releases_the_lock() {
    let (dir, mut repos, mut request) = repo_with_writer_trailer(None);
    let repo = repos.get_mut("demo").unwrap();
    repo.remote = "fixture:repo".into();
    let ssh = dir.join("stall-ssh");
    pg_worker_core::executable_test_support::write_private_executable(
        &ssh,
        "#!/bin/sh\nprintf '%s %s\\n' \"$PPID\" \"$$\" > fetch-pids\nexec sleep 8\n",
    );
    for args in [
        ["remote", "add", "origin", repo.remote.as_str()].as_slice(),
        &["config", "core.sshCommand", "./stall-ssh"],
        &["config", "ssh.variant", "simple"],
    ] {
        assert!(pg_worker_core::git_test_support::command(&dir)
            .args(args)
            .status()
            .unwrap()
            .success());
    }
    request["card"]["diff_ref"]["head_sha"] = json!("0".repeat(40));
    let diff = serde_json::from_value(request["card"]["diff_ref"].clone()).unwrap();
    let timeout = Duration::from_secs(1);
    let start = Instant::now();
    let result = crate::prompt_git::head_writers_with_timeout(repo, &diff, timeout);
    let elapsed = start.elapsed();
    let pids = std::fs::read_to_string(dir.join("fetch-pids")).expect("fetch reached the stub");
    let (git_pid, stub_pid) = pids.trim().split_once(' ').unwrap();
    let git_pid: libc::pid_t = git_pid.parse().unwrap();
    let stub_pid: libc::pid_t = stub_pid.parse().unwrap();
    let git_status = unsafe { libc::kill(git_pid, 0) };
    let git_error = std::io::Error::last_os_error();
    let lock = pg_worker_core::lockfile::try_lock(&dir.join(".git/spawner-fetch.lock")).unwrap();
    if git_status == 0 {
        // Clean up the fixture even when testing the old, detached timeout path.
        unsafe { libc::kill(stub_pid, libc::SIGKILL) };
    }
    assert_eq!(result, Err(crate::prompt::InlineDiffError::Git));
    assert!(
        (timeout..Duration::from_secs(3)).contains(&elapsed),
        "fetch took {elapsed:?}"
    );
    assert!(lock.is_some(), "fetch lock survived the refusal");
    assert_eq!(
        (git_status, git_error.raw_os_error()),
        (-1, Some(libc::ESRCH)),
        "Git was not reaped before the refusal"
    );
}

#[test]
fn native_codex_review_client_missing_has_a_distinct_exit() {
    let (dir, _) = fixture();
    let python = which_python();
    let sources = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../src");
    pg_worker_core::executable_test_support::write_private_executable(
        dir.join("installed-codex"),
        "#!/bin/sh\nprintf configured-codex",
    );
    let code = r#"
import io, json, pathlib, subprocess, sys
from plangraph.spawner.launch_spec import main
from plangraph.routing_support.clients import resolve_codex_client
root = pathlib.Path(sys.argv[1])
request = dict(run_id='run-'+'a'*32, job_id='fixture', role='review', dry_run=False,
    leg={'id':'gpt-6.2-sol-medium'}, card={}, cwd=str(root), state=str(root), prompt='review')
out, err = io.StringIO(), io.StringIO()
status = main(io.BytesIO(json.dumps(request).encode()), out, err)
assert status == 10, (status, err.getvalue())
assert err.getvalue().startswith('REVIEW ' + request['run_id'] + ' FAIL client_missing ')
client = root / 'installed-codex'
resolved, _ = resolve_codex_client({'PATH':'', 'PLANGRAPH_CODEX_BIN':str(client)})
assert subprocess.check_output([resolved], env={'PATH':''}) == b'configured-codex'
"#;
    let output = std::process::Command::new(python)
        .args(["-c", code])
        .arg(&dir.0)
        .env("PYTHONPATH", sources)
        .env("PATH", "")
        .env("HOME", &dir.0)
        .env_remove("PLANGRAPH_CODEX_BIN")
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
}

fn which_python() -> std::path::PathBuf {
    std::env::split_paths(&std::env::var_os("PATH").unwrap_or_default())
        .map(|dir| dir.join("python3"))
        .find(|file| file.is_file())
        .expect("Python compatibility adapter")
}

#[test]
fn review_route_admits_bounded_mimo_pro_inline() {
    let sources = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../src");
    let (dir, _, request) = repo_with_writer_trailer(Some("codex"));
    let script = r#"
import json, sys
from plangraph.planner.legs import DEFAULT_LEGS, REVIEW_HYDROGEN, REVIEW_MIMO_PRO
from plangraph.planner.route_policy import DEFAULT_POLICY_PATH, load_route_policy
from plangraph.planner.routing import plan_spawn
policy = load_route_policy(DEFAULT_POLICY_PATH)
ref = json.loads(sys.argv[1])
def route(**card):
    return plan_spawn(dict(estimate_tokens=2000, **card), role='review', writer_families=('gpt',),
        policy=policy, catalog_rows=(), leg_reader=lambda *a, **k: dict(state='available', evidence=None))
plan = route(diff_ref=ref)
ids = [leg.id for leg in plan.legs]
assert ids.index(REVIEW_HYDROGEN) < ids.index(REVIEW_MIMO_PRO), ids
assert plan.legs[ids.index(REVIEW_MIMO_PRO)].tool_mode == 'inline-only'
assert plan.legs[ids.index(REVIEW_MIMO_PRO)].max_request_tokens == 200_000
assert any(row['leg'] == REVIEW_MIMO_PRO and row['reason'] == 'inline_only_needs_diff' for row in route().skipped)
assert any(row['leg'] == REVIEW_MIMO_PRO and row['reason'] == 'repository_review_tools_unavailable'
    for row in route(diff_ref=ref, repository_review_required=True).skipped)
pro = next(leg for leg in DEFAULT_LEGS if leg.id == REVIEW_MIMO_PRO)
assert policy.skip_reason(pro, 'implement') is None and policy.skip_reason(pro, 'review') is None
"#;
    let registered = &request["card"]["diff_ref"];
    let reference =
        json!({"repo":dir.0,"base":registered["base_sha"],"head":registered["head_sha"]});
    let output = std::process::Command::new(which_python())
        .args(["-c", script])
        .arg(reference.to_string())
        .env("PYTHONPATH", sources)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
}

#[test]
fn review_route_mimo_not_served_on_host_advances_to_next_seat() {
    const CHILD: &str = "PG_TEST_MIMO_HOST_REFUSAL";
    if let Some(root) = std::env::var_os(CHILD) {
        use pg_worker_core::run::{
            group::Confinement, supervise::Config, supervisor::SupervisorCfg,
        };
        let root = std::path::PathBuf::from(root);
        let state = root.join("state");
        let args: crate::placement::Args =
            serde_json::from_slice(&std::fs::read(root.join("args")).unwrap()).unwrap();
        let runs = crate::runs::Runs::new(std::sync::Arc::new(
            pg_worker_core::run::supervisor::Supervisor::new(SupervisorCfg::new(Config {
                state_dir: state.clone(),
                confinement: Confinement::Plain,
                ..Config::default()
            }))
            .unwrap(),
        ));
        let result = crate::spawn::spawn(&state, &args, "fixture", &runs)
            .unwrap_or_else(|error| panic!("{}", error.into_error_data().message));
        assert_eq!(result["chosen_leg"]["id"], "next", "{result}");
        let requests: Vec<Value> = std::fs::read_to_string(root.join("requests"))
            .unwrap()
            .lines()
            .map(|line| serde_json::from_str(line).unwrap())
            .collect();
        assert_eq!(requests.len(), 2);
        assert_eq!(requests[0]["leg"]["id"], "mimo-v2.6-pro");
        assert_eq!(requests[1]["leg"]["id"], "next");
        assert!(requests
            .iter()
            .all(|request| request["card"]["diff_ref"] == args["card"]["diff_ref"]));
        let rows = crate::records::list_runs(&state);
        let failed = rows
            .iter()
            .find(|row| row["reason"] == "not_served_on_host")
            .unwrap();
        assert_eq!(failed["attempts"][0]["leg"]["id"], "mimo-v2.6-pro");
        assert_eq!(failed["head_sha"], args["card"]["diff_ref"]["head"]);
        assert_eq!(failed["review_base"], args["card"]["diff_ref"]["base"]);
        assert_eq!(
            result["plan"]["skipped"][0]["refusal_run"],
            failed["run_id"]
        );
        return;
    }
    let (dir, _, request) = repo_with_writer_trailer(Some("codex"));
    let refs = &request["card"]["diff_ref"];
    let args = json!({"prompt":"fixture","role":"review","writer_families":["gpt"],"wait_seconds":2,
        "card":{"estimate_tokens":1000,"diff_ref":{"repo":dir.0,"base":refs["base_sha"],"head":refs["head_sha"]}}});
    std::fs::write(dir.join("args"), args.to_string()).unwrap();
    let mimo = json!({"id":"mimo-v2.6-pro","model":"mimo/mimo-v2.6-pro","harness":"opencode","family":"mimo","tool_mode":"inline-only"});
    let next = json!({"id":"next","model":"fixture/next","harness":"opencode","family":"deepseek","tool_mode":"tools"});
    let plan = json!({"first_leg":mimo,"legs":[mimo,next],"skipped":[],
        "seat_admission":{"legs":{"mimo-v2.6-pro":1,"next":1}}});
    std::fs::write(dir.join("plan"), plan.to_string()).unwrap();
    let route = dir.join("route");
    let launch = dir.join("launch");
    pg_worker_core::executable_test_support::write_private_executable(
        &route,
        "#!/bin/sh\ncat plan\n",
    );
    let python = which_python();
    pg_worker_core::executable_test_support::write_private_executable(
        &launch,
        format!(
            r#"#!{}
import json, pathlib, sys
request = json.load(sys.stdin)
with open('requests', 'a') as output:
    output.write(json.dumps(request) + '\n')
slots = pathlib.Path('state/seat-slots/mimo-v2_6-pro')
held = [p.name for p in slots.iterdir() if p.name != 'admission.lock']
if request['leg']['id'] == 'mimo-v2.6-pro':
    assert len(held) == 1, held
    print('unavailable: not_served_on_host: fixture', file=sys.stderr)
    sys.exit(2)
assert not held, held
assert len([p for p in pathlib.Path('state/seat-slots/next').iterdir() if p.name != 'admission.lock']) == 1
print(json.dumps(dict(argv=['/bin/echo', 'VERDICT: PASS\nFILES READ: f'], cwd=request['cwd'],
                      env={{}}, stdin=request['prompt'], prompt=request['prompt'])))
"#,
            python.display()
        ),
    );
    let test_name = format!(
        "{}::review_route_mimo_not_served_on_host_advances_to_next_seat",
        module_path!()
    )
    .split_once("::")
    .unwrap()
    .1
    .to_owned();
    let output = std::process::Command::new(std::env::current_exe().unwrap())
        .current_dir(&*dir)
        .args(["--exact", &test_name, "--nocapture"])
        .env(CHILD, &dir.0)
        .env("PG_SPAWND_ADAPTER", route)
        .env("PG_SPAWND_LAUNCH_CMD", launch)
        .env("XDG_CONFIG_HOME", dir.join("config"))
        .output()
        .unwrap();
    assert!(
        output.status.success() && String::from_utf8_lossy(&output.stdout).contains("1 passed"),
        "{}\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
}

#[test]
fn a_non_ascii_registered_head_refuses_without_echoing_the_head() {
    let (_dir, repos, mut request) = repo_with_writer_trailer(None);
    let head = format!("{}é{}", "a".repeat(11), "a".repeat(27));
    assert_eq!(head.len(), 40);
    assert!(!head.is_char_boundary(12));
    request["card"]["diff_ref"]["head_sha"] = json!(head);
    assert!(matches!(
        with_head_writers(&request, &repos),
        Err(crate::errors::ToolError::Refused { kind: RefusalKind::Spawn, message, .. })
            if message == "cannot read the Writer trailer of the registered head"
    ));
}

#[test]
fn unresolved_registered_refs_refuse_without_echoing_or_starting_the_adapter() {
    let (dir, spec) = fixture();
    let marker = dir.join("started");
    let adapter = script(
        &dir,
        &format!("touch {}; printf '%s' '{}'", marker.display(), spec),
    );
    for diff_ref in [
        json!({"repo_id":"nowhere","base_sha":"a".repeat(40),"head_sha":"b".repeat(40)}),
        json!({"repo_id":"nowhere"}),
    ] {
        let request =
            json!({"role":"review","writer_families":["openai"],"card":{"diff_ref":diff_ref}});
        let error = adapter
            .request(&dir, &request, Duration::from_secs(5))
            .err()
            .expect("refusal");
        match error {
            crate::errors::ToolError::Refused { message, .. } => {
                assert!(!message.contains(&"b".repeat(12)), "{message}");
            }
            _ => panic!("expected a refusal"),
        }
        assert!(
            !marker.exists(),
            "the adapter child started before the lookup"
        );
    }
}

#[test]
fn deepseek_review_uses_the_catalogued_output_ceiling() {
    let sources = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../src");
    let code = r#"
from plangraph.spawner.opencode_answer import configure_launch_output
from plangraph.catalog_data import SEED_MODELS
model = 'opencode-go/deepseek-v4.1-flash'
limit = next(row['max_output_tokens'] for row in SEED_MODELS if row['model_id'] == model)
review_env = {}
assert configure_launch_output(review_env, model, 'review', False) is None
assert review_env['OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX'] == str(limit)
writer_env = {}
assert configure_launch_output(writer_env, model, 'implement', False) is None
assert writer_env == {}
"#;
    let output = std::process::Command::new(which_python())
        .args(["-c", code])
        .env("PYTHONPATH", sources)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
}
