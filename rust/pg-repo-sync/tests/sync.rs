use std::{
    fs,
    path::Path,
    process::{Command, Output},
};
fn git(repo: &Path, args: &[&str]) -> String {
    let mut cmd = Command::new("git");
    for (key, _) in std::env::vars_os() {
        if key.to_string_lossy().starts_with("GIT_") {
            cmd.env_remove(key);
        }
    }
    let out = cmd
        .current_dir(repo)
        .args([
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
        ])
        .args(args)
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    String::from_utf8(out.stdout).unwrap().trim().into()
}
fn fixture() -> tempfile::TempDir {
    let tmp = tempfile::tempdir().unwrap();
    let seed = tmp.path().join("source");
    fs::create_dir(&seed).unwrap();
    git(&seed, &["init", "-qb", "main"]);
    fs::write(seed.join("file"), "one").unwrap();
    git(&seed, &["add", "."]);
    git(&seed, &["commit", "-qm", "one"]);
    git(tmp.path(), &["clone", "-q", "source", "checkout"]);
    fs::write(seed.join("file"), "two").unwrap();
    git(&seed, &["commit", "-qam", "two"]);
    tmp
}
fn run(cwd: &Path, args: &[&str]) -> Output {
    Command::new(env!("CARGO_BIN_EXE_pg-repo-sync"))
        .current_dir(cwd)
        .args(args)
        .output()
        .unwrap()
}
#[test]
fn all_five_reasons_and_explicit_label() {
    for reason in [
        "detached",
        "other-branch:topic",
        "tracked-changes",
        "diverged",
        "fetch-failed",
    ] {
        let tmp = fixture();
        let repo = tmp.path().join("checkout");
        match reason {
            "detached" => {
                git(&repo, &["checkout", "-q", "--detach"]);
            }
            "other-branch:topic" => {
                git(&repo, &["checkout", "-qb", "topic"]);
            }
            "tracked-changes" => {
                fs::write(repo.join("file"), "dirty").unwrap();
            }
            "diverged" => {
                fs::write(repo.join("local"), "local").unwrap();
                git(&repo, &["add", "."]);
                git(&repo, &["commit", "-qm", "local"]);
            }
            _ => {
                git(&repo, &["remote", "set-url", "origin", "missing-origin"]);
            }
        }
        let out = run(tmp.path(), &["--repo", "checkout"]);
        assert_eq!(out.status.code(), Some(1));
        let text = String::from_utf8(out.stdout).unwrap();
        assert_eq!(text.lines().count(), 1);
        assert!(text.starts_with("ALERT checkout "), "{text}");
        assert!(text.ends_with(&format!("reason={reason}\n")), "{text}");
        assert!(!text.contains("behind=0"));
    }
}
#[test]
fn fast_forward_and_missing_home_or_code_are_silent() {
    let tmp = fixture();
    let out = run(tmp.path(), &["--repo", "checkout"]);
    assert!(out.status.success() && out.stdout.is_empty() && out.stderr.is_empty());
    assert_eq!(
        git(&tmp.path().join("checkout"), &["rev-parse", "HEAD"]),
        git(&tmp.path().join("source"), &["rev-parse", "HEAD"])
    );
    for home in [None, Some(tmp.path())] {
        let mut cmd = Command::new(env!("CARGO_BIN_EXE_pg-repo-sync"));
        cmd.current_dir(tmp.path()).env_remove("HOME");
        if let Some(home) = home {
            cmd.env("HOME", home);
        }
        let out = cmd.output().unwrap();
        assert!(out.status.success() && out.stdout.is_empty() && out.stderr.is_empty());
    }
}
#[test]
fn direct_children_and_symlink_children_are_discovered() {
    let tmp = fixture();
    let root = tmp.path().join("code");
    fs::create_dir(&root).unwrap();
    fs::rename(tmp.path().join("checkout"), root.join("checkout")).unwrap();
    let repo = root.join("checkout");
    git(
        &repo,
        &[
            "remote",
            "set-url",
            "origin",
            tmp.path().join("source").to_str().unwrap(),
        ],
    );
    fs::write(repo.join("file"), "dirty").unwrap();
    #[cfg(unix)]
    std::os::unix::fs::symlink(&repo, root.join("linked")).unwrap();
    let out = run(tmp.path(), &["--root", root.to_str().unwrap()]);
    assert_eq!(out.status.code(), Some(1));
    let text = String::from_utf8(out.stdout).unwrap();
    assert_eq!(text.lines().count(), if cfg!(unix) { 2 } else { 1 });
    assert!(text.contains("checkout") && text.contains("reason=tracked-changes"));
    #[cfg(unix)]
    assert!(text.contains("linked"));
}

#[test]
fn failed_fast_forward_is_an_alert_with_host_and_path() {
    let tmp = fixture();
    let source = tmp.path().join("source");
    fs::write(source.join("collision"), "remote").unwrap();
    git(&source, &["add", "."]);
    git(&source, &["commit", "-qm", "collision"]);
    fs::write(tmp.path().join("checkout/collision"), "local").unwrap();
    let out = run(
        tmp.path(),
        &[
            "--repo",
            "checkout",
            "--sync",
            "--check-config",
            "--host",
            "worker",
            "--allow-name",
            "Fixture",
            "--allow-email",
            "test@example.invalid",
        ],
    );
    assert_eq!(out.status.code(), Some(1));
    let text = String::from_utf8(out.stdout).unwrap();
    assert!(text.starts_with("ALERT host=worker path=\"checkout\" reason=diverged"));
    assert_eq!(
        fs::read_to_string(tmp.path().join("checkout/collision")).unwrap(),
        "local"
    );
}
