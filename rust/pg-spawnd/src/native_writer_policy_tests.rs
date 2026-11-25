//! Run the production language-gate wrapper against a recording policy executable.
use super::*;

fn install(repo: &Path) {
    let gate = include_str!("../../../tools/check_new_code_lang.py");
    fs::write(
        repo.join("tools/check_new_code_lang.py"),
        format!(
            "import os\nos.environ['PG_POLICY_BIN'] = os.path.abspath('tools/fixture-pg-policy')\n{gate}"
        ),
    )
    .unwrap();

    // The native gate takes the policy build lease when this manifest is present.
    let crate_dir = repo.join("rust/pg-policy");
    fs::create_dir_all(&crate_dir).unwrap();
    fs::write(crate_dir.join("Cargo.toml"), "").unwrap();
    let policy = repo.join("tools/fixture-pg-policy");
    pg_worker_core::executable_test_support::write_executable(
        &policy,
        r#"#!/usr/bin/env python3
import json, os, pathlib, subprocess, sys
if sys.platform.startswith('linux'):
    import fcntl
    target = os.environ['CARGO_TARGET_DIR']
    with open(target + '.plangraph-lock', 'rb') as lease:
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            raise AssertionError('native policy gate did not hold the cargo lease')

args = sys.argv[1:]
if args[:1] == ['--class-search']:
    record = json.loads((pathlib.Path.cwd().parent / 'expected-policy.json').read_text())
    pin = 'refs/plangraph/native/' + record['run_id'] + '/base'
    assert args == ['--class-search', '--range', pin], args
    actual = subprocess.check_output(['git', 'rev-parse', pin], text=True).strip()
    assert actual == record['base_sha'], (actual, record['base_sha'])
    print('policy invocation: ' + ' '.join(args))
    if (pathlib.Path.cwd().parent / 'reject-policy').is_file():
        sys.exit('malformed Class-Search from fixture')
"#,
    );
}

#[test]
fn native_class_search_uses_the_pinned_range_under_the_policy_lease() {
    const FIXTURE: &str = "PG_NATIVE_POLICY_FIXTURE";
    let Some(root) = std::env::var_os(FIXTURE) else {
        let root = tempfile::tempdir().unwrap();
        let name = std::thread::current();
        let mut child = Command::new(std::env::current_exe().unwrap());
        pg_worker_core::git_test_support::configure(&mut child, root.path());
        let result = child
            .env_remove("CLASS_SEARCH_BASE")
            .args(["--exact", name.name().unwrap(), "--nocapture"])
            .env(FIXTURE, root.path())
            .env(
                "PLANGRAPH_CARGO_TARGET_DIR",
                root.path().join("cargo-target"),
            )
            .output()
            .unwrap();
        let stdout = String::from_utf8_lossy(&result.stdout);
        assert!(
            result.status.success() && stdout.contains("1 passed; 0 failed"),
            "stdout:\n{stdout}\nstderr:\n{}",
            String::from_utf8_lossy(&result.stderr)
        );
        return;
    };

    let root = PathBuf::from(root);
    let cwd = root.join("worktree");
    fs::create_dir_all(cwd.join("tools")).unwrap();
    install(&cwd);
    let git =
        |args: &[&str]| output(pg_worker_core::git_test_support::command(&cwd).args(args)).unwrap();
    git(&["init", "-q"]);
    git(&["add", "."]);
    git(&["commit", "-qm", "base"]);
    let base = git(&["rev-parse", "HEAD"]);
    let host = Host {
        repo: cwd.clone(),
        run_id: "run-policy-fixture".into(),
        cwd: cwd.clone(),
        reviews: root.join("reviews"),
        record_path: root.join("record.json"),
        base_ref: "refs/heads/main".into(),
        tool_path: std::env::var("PATH").unwrap(),
        effort: WRITER_EFFORT.into(),
        state: root.clone(),
        brief: "brief".into(),
        push_ref: None,
        fast_checks_stand_in: None,
        reviews_started: 0,
        review_override: None,
        review_cli: vec!["false".into()],
        python_src: None,
    };
    let pin = host.base_pin();
    git(&["update-ref", &pin, &base]);
    git(&["commit", "--allow-empty", "-qm", "writer head"]);
    fs::write(
        root.join("expected-policy.json"),
        json!({"base_sha": base, "run_id": &host.run_id}).to_string(),
    )
    .unwrap();

    let log = host.checks().unwrap();
    assert!(
        log.contains(&format!("policy invocation: --class-search --range {pin}")),
        "{log}"
    );
    fs::write(root.join("reject-policy"), "").unwrap();
    let refusal = host.checks().unwrap_err();
    assert!(
        refusal.contains("malformed Class-Search from fixture"),
        "{refusal}"
    );
    let target = root.join("cargo-target");
    assert!(pg_worker_core::cargo_cache::try_exclusive_lease(&target)
        .unwrap()
        .is_some());
}
