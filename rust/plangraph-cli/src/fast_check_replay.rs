//! Verify explicit base-failure claims without changing the lane checkout.
use super::Result;
use pg_worker_core::workspace_resolver::git_command;
use std::path::{Component, Path};
use std::process::Command;

pub(super) fn scrub_test_env(command: &mut Command) {
    for (key, _) in std::env::vars_os() {
        let name = key.to_string_lossy().to_ascii_uppercase();
        if [
            "AUTOOS_CTX_",
            "CLAUDE_",
            "CODEX_",
            "FLEET_",
            "ROUTING_SESSION_",
        ]
        .iter()
        .any(|prefix| name.starts_with(prefix))
            || name.split('_').any(|part| part == "SESSION")
            || matches!(name.as_str(), "PYTEST_ADDOPTS" | "PYTHONPATH")
        {
            command.env_remove(key);
        }
    }
}

fn git(root: &Path, args: &[&str]) -> Result<String> {
    let result = git_command(root)
        .args(args)
        .output()
        .map_err(|e| e.to_string())?;
    if !result.status.success() {
        return Err(format!(
            "{}: {}",
            args[0],
            String::from_utf8_lossy(&result.stderr)
        ));
    }
    String::from_utf8(result.stdout).map_err(|e| e.to_string())
}

fn valid_node(node: &str) -> bool {
    let Some((path, test)) = node.split_once("::") else {
        return false;
    };
    !path.starts_with('-')
        && path.ends_with(".py")
        && Path::new(path)
            .components()
            .all(|part| matches!(part, Component::Normal(_)))
        && !test.is_empty()
        && !node.chars().any(char::is_control)
}

pub(super) fn verify(root: &Path, base: &str) -> Result<()> {
    let sha = git(
        root,
        &[
            "rev-parse",
            "--verify",
            "--end-of-options",
            &format!("{base}^{{commit}}"),
        ],
    )?;
    let sha = sha.trim();
    let claims = git(
        root,
        &[
            "log",
            "--format=%(trailers:key=Fails-at-Base,valueonly,unfold)",
            &format!("{sha}..HEAD"),
        ],
    )?;
    let nodes = claims
        .lines()
        .filter(|line| !line.trim().is_empty())
        .map(|line| match line.trim().split_once(' ') {
            Some((pin, node)) if pin == sha && valid_node(node) => Ok(node),
            _ => Err(format!(
                "invalid Fails-at-Base trailer (expected {sha} and a pytest node ID)"
            )),
        })
        .collect::<Result<std::collections::BTreeSet<_>>>()?;
    if nodes.is_empty() {
        return Ok(());
    }
    let checkout = tempfile::tempdir().map_err(|e| e.to_string())?;
    let path = checkout
        .path()
        .to_str()
        .ok_or("base checkout path must be UTF-8")?;
    git(
        root,
        &[
            "clone",
            "--quiet",
            "--shared",
            "--no-checkout",
            "--",
            ".",
            path,
        ],
    )?;
    git(checkout.path(), &["checkout", "--quiet", "--detach", sha])?;
    for node in nodes {
        replay(checkout.path(), sha, node)?;
    }
    Ok(())
}

fn replay(checkout: &Path, sha: &str, node: &str) -> Result<()> {
    let mut command = Command::new("python3");
    command
        .args(["-m", "pytest", "-q", "--color=no", "--", node])
        .current_dir(checkout);
    scrub_test_env(&mut command);
    println!("Fails-at-Base {sha}: python3 -m pytest -q --color=no -- {node:?}");
    let output = command.output().map_err(|e| e.to_string())?;
    let stdout = String::from_utf8_lossy(&output.stdout);
    print!("{stdout}");
    eprint!("{}", String::from_utf8_lossy(&output.stderr));
    match output.status.code() {
        Some(0) => Err(format!("{node} passed at base {sha}")),
        Some(1)
            if stdout.lines().any(|line| {
                line.strip_prefix("FAILED ").is_some_and(|failed| {
                    failed == node
                        || failed
                            .strip_prefix(node)
                            .is_some_and(|rest| rest.starts_with(" - "))
                })
            }) =>
        {
            Ok(())
        }
        _ => Err(format!(
            "could not reproduce failure of {node} at base {sha}"
        )),
    }
}
