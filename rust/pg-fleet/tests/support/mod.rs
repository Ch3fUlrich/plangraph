use std::path::Path;
use std::process::Command;

/// All callers start without any GIT_* from the host. The workspace sandbox
/// helper also removes credentials and supplies the private HOME.
pub fn isolated(program: &str, home: &Path) -> Command {
    std::fs::create_dir_all(home).unwrap();
    let mut c = Command::new(program);
    pg_worker_core::sandbox_env::confine_check(&mut c, home);
    // Windows exports this as "Path"; the shared helper's exact-name allowlist
    // does not retain that spelling. Reinsert the nonsecret search path.
    if let Some(path) = std::env::var_os("PATH") {
        c.env("PATH", path);
    }
    if let Some(root) = std::env::var_os("SystemRoot") {
        c.env("SystemRoot", root);
    }
    c.env("USERPROFILE", home)
        .env("GIT_CONFIG_GLOBAL", home.join("gitconfig"))
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_AUTHOR_NAME", "fixture")
        .env("GIT_AUTHOR_EMAIL", "fixture@example.invalid")
        .env("GIT_COMMITTER_NAME", "fixture")
        .env("GIT_COMMITTER_EMAIL", "fixture@example.invalid");
    c
}
pub fn git(dir: &Path, home: &Path, args: &[&str]) -> String {
    let out = isolated("git", home)
        .current_dir(dir)
        .args(args)
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "git {args:?}: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    String::from_utf8_lossy(&out.stdout).trim().into()
}
