use super::*;
#[test]
#[serial_test::serial(env)]
fn pilot_no_push_credentials() {
    let root = tempfile::tempdir().unwrap();
    let source = root.path().join("source");
    std::fs::create_dir_all(source.join(".ssh")).unwrap();
    std::fs::write(source.join(".ssh/id_ed25519"), "private").unwrap();
    std::fs::write(source.join(".git-credentials"), "credential").unwrap();
    let names = [
        "GIT_ASKPASS",
        "GIT_SSH_COMMAND",
        "SSH_AUTH_SOCK",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "GITLAB_TOKEN",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_COUNT",
        "PG_PILOT_CREDENTIAL",
    ];
    let saved: Vec<_> = names.iter().map(|n| (*n, std::env::var_os(n))).collect();
    let old_auth = std::env::var_os(HARNESS_AUTH_DIR_ENV);
    for name in names {
        std::env::set_var(name, "pilot-parent");
    }
    std::env::set_var(HARNESS_AUTH_DIR_ENV, &source);
    let dir = root.path().join("work");
    std::fs::create_dir(&dir).unwrap();
    let mut home = HomeGuard::for_dir(&dir).unwrap();
    home.stage_harness_auth().unwrap();
    for confine in [confine_harness, confine_check] {
        let mut cmd = Command::new("sh");
        cmd.current_dir(&dir).args(["-c", "env; test ! -e \"$HOME/.git-credentials\" && test ! -e \"$HOME/.ssh\" && git init -q && test -z \"$(git remote)\" && ! git push"]);
        confine(&mut cmd, home.path());
        let out = cmd.output().unwrap();
        assert!(
            out.status.success(),
            "{}",
            String::from_utf8_lossy(&out.stderr)
        );
        let env = String::from_utf8_lossy(&out.stdout);
        for name in names {
            assert!(!env.lines().any(|l| l.starts_with(&format!("{name}="))));
            assert_eq!(std::env::var(name).unwrap(), "pilot-parent");
        }
    }
    for (name, value) in saved {
        match value {
            Some(v) => std::env::set_var(name, v),
            None => std::env::remove_var(name),
        }
    }
    match old_auth {
        Some(v) => std::env::set_var(HARNESS_AUTH_DIR_ENV, v),
        None => std::env::remove_var(HARNESS_AUTH_DIR_ENV),
    }
}
