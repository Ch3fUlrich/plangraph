//! SPAWNER-PACKAGE-SESSIONS: render one authoritative contract and gate completion.
use serde::Deserialize;
use std::{io::Read, path::PathBuf, process::Command};

#[derive(Deserialize)]
struct Request {
    operation: String,
    repo: PathBuf,
    #[serde(default)]
    base: String,
    #[serde(default)]
    key: String,
    #[serde(default)]
    writer: String,
    #[serde(default)]
    package: String,
    #[serde(default)]
    body: String,
}

fn gate() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tools/ci_commit_gate.py")
}

fn repo_command(executable: &str, repo: &std::path::Path) -> Command {
    let mut command = Command::new(executable);
    command.current_dir(repo);
    for (key, _) in std::env::vars_os() {
        if key
            .to_string_lossy()
            .to_ascii_uppercase()
            .starts_with("GIT_")
        {
            command.env_remove(key);
        }
    }
    #[cfg(test)]
    pg_worker_core::git_test_support::configure(&mut command, repo);
    command
}

fn execute(request: Request) -> Result<String, String> {
    let python = std::env::var("PREFLIGHT_PYTHON").unwrap_or_else(|_| "python3".into());
    let mut command = repo_command(&python, &request.repo);
    command.arg(gate()).current_dir(&request.repo);
    match request.operation.as_str() {
        "render" => {
            for value in [&request.key, &request.writer, &request.package] {
                if value.trim().is_empty() || value.contains(['\n', '\r', '{', '}']) {
                    return Err("package: invalid template value".into());
                }
            }
            let result = command
                .arg("package-rules")
                .output()
                .map_err(|e| e.to_string())?;
            if !result.status.success() {
                return Err("package: policy unavailable".into());
            }
            let policy: serde_json::Value =
                serde_json::from_slice(&result.stdout).map_err(|_| "package: invalid policy")?;
            let template = policy["template"]
                .as_str()
                .ok_or("package: missing template")?;
            Ok(format!(
                "{}\n{}",
                template
                    .replace("{key}", &request.key)
                    .replace("{writer}", &request.writer)
                    .replace("{package}", &request.package),
                request.body
            ))
        }
        "check" => {
            if request.base.is_empty() {
                return Err("package: base required".into());
            }
            let result = command
                .args(["check", "--base", &request.base, "--head", "HEAD"])
                .output()
                .map_err(|e| e.to_string())?;
            if !result.status.success() {
                return Err(format!(
                    "package: commit gate refused\n{}{}",
                    String::from_utf8_lossy(&result.stdout),
                    String::from_utf8_lossy(&result.stderr)
                ));
            }
            let result = repo_command("git", &request.repo)
                .args([
                    "log",
                    "--no-merges",
                    "--format=%H%x00%(trailers:key=Plan-Item,valueonly)%x00",
                    &format!("{}..HEAD", request.base),
                ])
                .output()
                .map_err(|e| e.to_string())?;
            if !result.status.success() {
                return Err("package: branch inspection failed".into());
            }
            let log = String::from_utf8_lossy(&result.stdout);
            let fields: Vec<_> = log.split('\0').collect();
            for pair in fields.as_chunks::<2>().0 {
                let values: Vec<_> = pair[1].lines().collect();
                if values.len() != 1
                    || !values[0].starts_with("plangraph:")
                    || values[0].trim() != values[0]
                    || values[0].len() <= 10
                {
                    return Err(format!(
                        "package: missing or malformed Plan-Item: {}",
                        pair[0].trim()
                    ));
                }
            }
            Ok("ALL DONE".into())
        }
        _ => Err("package: unknown operation".into()),
    }
}

fn main() {
    let mut input = String::new();
    let result = std::io::stdin()
        .take(1024 * 1024)
        .read_to_string(&mut input)
        .map_err(|e| e.to_string())
        .and_then(|_| serde_json::from_str(&input).map_err(|e| e.to_string()))
        .and_then(execute);
    match result {
        Ok(text) => println!("{text}"),
        Err(error) => {
            eprintln!("{error}");
            std::process::exit(1);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use pg_worker_core::git_test_support;

    #[test]
    fn package_missing_trailer_refuses_before_all_done() {
        let repo = tempfile::tempdir().unwrap();
        let git = |args: &[&str]| {
            let result = git_test_support::command(repo.path())
                .args(args)
                .output()
                .unwrap();
            assert!(
                result.status.success(),
                "{}",
                String::from_utf8_lossy(&result.stderr)
            );
        };
        git(&["init", "-q"]);
        std::fs::create_dir(repo.path().join("tools")).unwrap();
        std::fs::write(repo.path().join("tools/ci_commit_exempt.txt"), "").unwrap();
        git(&["add", "."]);
        git(&["commit", "-qm", "base"]);
        git(&[
            "commit",
            "--allow-empty",
            "-qm",
            "missing writer\n\nPlan-Item: plangraph:TEST",
        ]);
        let request = || Request {
            operation: "check".into(),
            repo: repo.path().into(),
            base: "HEAD~1".into(),
            key: String::new(),
            writer: String::new(),
            package: String::new(),
            body: String::new(),
        };
        assert!(execute(request()).unwrap_err().contains("missing Writer"));
        git(&[
            "commit",
            "--amend",
            "--allow-empty",
            "-qm",
            "missing plan\n\nWriter: codex/test",
        ]);
        assert!(execute(request()).unwrap_err().contains("Plan-Item"));
        git(&[
            "commit",
            "--amend",
            "--allow-empty",
            "-qm",
            "complete\n\nPlan-Item: plangraph:TEST\nWriter: codex/test",
        ]);
        assert_eq!(execute(request()).unwrap(), "ALL DONE");
    }

    #[test]
    fn rendered_package_contains_every_gate_trailer() {
        let repo = tempfile::tempdir().unwrap();
        let text = execute(Request {
            operation: "render".into(),
            repo: repo.path().into(),
            base: String::new(),
            key: "TEST".into(),
            writer: "codex/test".into(),
            package: "W1".into(),
            body: "Implement the task".into(),
        })
        .unwrap();
        for trailer in [
            "Writer: codex/test",
            "Plan-Item: plangraph:TEST",
            "Gate-Edit:",
            "Test-Removal:",
            "Hand-Edit:",
        ] {
            assert!(text.contains(trailer), "{trailer}");
        }
        assert!(!text.contains("{key}"));
        assert!(text.ends_with("Implement the task"));
    }
}
