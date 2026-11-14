//! Keep the shared hook checkout on freshly fetched main; detect drift.
use serde_json::Value;
use std::path::Path;
fn git(path: &Path, args: &[&str]) -> Result<String, String> {
    let mut cmd = std::process::Command::new("git");
    cmd.arg("-C")
        .arg(path)
        .args(args)
        .env("GIT_TERMINAL_PROMPT", "0");
    for (key, _) in std::env::vars_os() {
        if key.to_string_lossy().starts_with("GIT_") {
            cmd.env_remove(key);
        }
    }
    cmd.env("GIT_TERMINAL_PROMPT", "0");
    let (code, out, _) =
        crate::package_transport::capture(&mut cmd, "", std::time::Duration::from_secs(120))
            .map_err(|e| e.into_error_data().message.to_string())?;
    if code != 0 {
        return Err("hook checkout Git command failed".into());
    }
    Ok(out.trim().into())
}
fn run(path: &Path, check_only: bool) -> Result<Value, String> {
    let branch = git(path, &["symbolic-ref", "--short", "HEAD"])?;
    if branch != "main" {
        return Err("hook checkout must follow main".into());
    }
    let dirty = !git(path, &["status", "--porcelain"])?.is_empty();
    if !check_only {
        if dirty {
            return Err("hook checkout dirty; sync refused".into());
        }
        git(
            path,
            &[
                "fetch",
                "--no-write-fetch-head",
                "origin",
                "+refs/heads/main:refs/remotes/origin/main",
            ],
        )?;
        git(
            path,
            &["merge-base", "--is-ancestor", "HEAD", "origin/main"],
        )?;
        git(path, &["merge", "--ff-only", "--no-edit", "origin/main"])?;
    }
    let behind = git(path, &["rev-list", "--count", "HEAD..origin/main"])?
        .parse::<u64>()
        .map_err(|_| "invalid drift count")?;
    let ahead = git(path, &["rev-list", "--count", "origin/main..HEAD"])?
        .parse::<u64>()
        .map_err(|_| "invalid drift count")?;
    Ok(
        serde_json::json!({"branch":branch,"behind":behind,"ahead":ahead,"dirty":dirty,"drift":behind>0 || ahead>0 || dirty,"synced":!check_only}),
    )
}
pub(crate) fn cli(args: Vec<String>, out: &mut dyn std::io::Write) -> Result<(), String> {
    if !(args.len() == 2 || args.len() == 3 && args[2] == "--check")
        || args[0] != "--hooks-checkout"
    {
        return Err("host-code-sync --hooks-checkout <main checkout> [--check]".into());
    }
    let result = run(Path::new(&args[1]), args.len() == 3)?;
    writeln!(out, "{result}").map_err(|e| e.to_string())?;
    if result["drift"] == true {
        return Err("hook checkout drift alarm".into());
    }
    Ok(())
}
#[cfg(test)]
mod tests {
    use super::*;
    use std::process::Command;
    fn git(repo: &Path, args: &[&str]) -> String {
        let o = Command::new("git")
            .arg("-C")
            .arg(repo)
            .args(args)
            .env("GIT_CONFIG_GLOBAL", "/dev/null")
            .env("GIT_CONFIG_NOSYSTEM", "1")
            .env("GIT_AUTHOR_NAME", "Fixture")
            .env("GIT_AUTHOR_EMAIL", "123+fixture@users.noreply.github.com")
            .env("GIT_COMMITTER_NAME", "Fixture")
            .env(
                "GIT_COMMITTER_EMAIL",
                "123+fixture@users.noreply.github.com",
            )
            .output()
            .unwrap();
        assert!(o.status.success(), "{}", String::from_utf8_lossy(&o.stderr));
        String::from_utf8_lossy(&o.stdout).trim().into()
    }
    #[test]
    fn host_code_sync_reports_drift_then_fast_forwards_fixture() {
        let dir = std::env::temp_dir().join(pg_worker_core::run::record::new_run_id());
        std::fs::create_dir(&dir).unwrap();
        let source = dir.join("source");
        std::fs::create_dir(&source).unwrap();
        git(&source, &["init", "-qb", "main"]);
        git(&source, &["commit", "--allow-empty", "-qm", "base"]);
        let hooks = dir.join("hooks");
        git(
            &dir,
            &[
                "clone",
                "-q",
                source.to_str().unwrap(),
                hooks.to_str().unwrap(),
            ],
        );
        git(&source, &["commit", "--allow-empty", "-qm", "new main"]);
        git(&hooks, &["fetch", "-q", "origin"]);
        assert_eq!(run(&hooks, true).unwrap()["behind"], 1);
        assert_eq!(
            cli(
                vec![
                    "--hooks-checkout".into(),
                    hooks.to_string_lossy().into(),
                    "--check".into()
                ],
                &mut Vec::new()
            )
            .unwrap_err(),
            "hook checkout drift alarm"
        );
        assert_eq!(run(&hooks, false).unwrap()["behind"], 0);
        assert_eq!(
            git(&hooks, &["rev-parse", "HEAD"]),
            git(&source, &["rev-parse", "HEAD"])
        );
        std::fs::remove_dir_all(dir).unwrap();
    }
}
