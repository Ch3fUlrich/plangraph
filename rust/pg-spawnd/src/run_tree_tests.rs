//! Cross-language request: real Runs JSON -> fresh_main -> real CLI launch-spec.
use super::*;
use std::io::Write;
use std::process::{Command, Stdio};

#[test]
fn real_request_reaches_python_launch_spec_with_state() {
    let dir = Dir(std::env::temp_dir().join(new_run_id()));
    std::fs::create_dir(&dir.0).unwrap();
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .parent()
        .unwrap();
    let runs = Runs::new(Arc::new(
        Supervisor::new(SupervisorCfg::new(Config {
            state_dir: dir.0.clone(),
            confinement: Confinement::Plain,
            ..Config::default()
        }))
        .unwrap(),
    ));
    let args = json!({"prompt":"task","card":{},"dry_run":true});
    let error = runs
        .spawn_using(
            &dir.0,
            args.as_object().unwrap(),
            "local",
            json!("review"),
            json!({"first_leg":{"id":"test-nonexistent-leg","family":"f"}}),
            |state, request| {
                assert_eq!(request["state"], state.to_string_lossy().as_ref());
                assert_eq!(request.as_object().unwrap().len(), 9);
                let mut child = Command::new("python3")
                    .args([
                        "-c",
                        r#"
import contextlib, pathlib, runpy
from plangraph.spawner import git_guard
@contextlib.contextmanager
def tree(state):
    assert pathlib.Path(state).is_absolute()
    yield dict(tree_sha='a' * 40, tree_path=str(pathlib.Path.cwd()))
git_guard.run_tree = tree
runpy.run_module('plangraph.spawner.launch_spec', run_name='__main__')
"#,
                    ])
                    .current_dir(root)
                    .env("PYTHONPATH", root.join("src"))
                    .stdin(Stdio::piped())
                    .stdout(Stdio::piped())
                    .stderr(Stdio::piped())
                    .spawn()
                    .unwrap();
                child
                    .stdin
                    .take()
                    .unwrap()
                    .write_all(&serde_json::to_vec(request).unwrap())
                    .unwrap();
                let output = child.wait_with_output().unwrap();
                assert_eq!(
                    output.status.code(),
                    Some(2),
                    "{}",
                    String::from_utf8_lossy(&output.stderr)
                );
                assert_eq!(output.stdout, b"");
                assert_eq!(output.stderr, b"unknown_leg: unknown leg id\n");
                Err(internal("contract exercised"))
            },
        )
        .err()
        .unwrap()
        .into_error_data();
    assert_eq!(error.message, "contract exercised");
}
