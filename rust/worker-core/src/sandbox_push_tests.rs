use super::*;
// not-applicable: auth staging already excludes .gitconfig; extend the fixture
// to the operator's GitHub helper and query git inside real confinement.
#[test]
#[serial_test::serial(env)]
fn pilot_no_push_credentials() {
    let root = tempfile::tempdir().unwrap();
    let source = root.path().join("source");
    std::fs::create_dir_all(source.join(".ssh")).unwrap();
    std::fs::write(source.join(".ssh/id_test"), "private").unwrap();
    std::fs::create_dir_all(source.join(".config/gh")).unwrap();
    std::fs::write(source.join(".config/gh/hosts.yml"), "credential").unwrap();
    std::fs::write(source.join("agent-auth"), "staged").unwrap();
    std::fs::write(source.join(".git-credentials"), "credential").unwrap();
    std::fs::write(
        source.join(".gitconfig"),
        "[credential \"https://github.com\"]\n helper = !gh auth git-credential\n",
    )
    .unwrap();
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
    let old_home = std::env::var_os("HOME");
    for name in names {
        std::env::set_var(name, "pilot-parent");
    }
    std::env::set_var(HARNESS_AUTH_DIR_ENV, &source);
    std::env::set_var("HOME", &source);
    let dir = root.path().join("work");
    std::fs::create_dir(&dir).unwrap();
    let mut home = HomeGuard::for_dir(&dir).unwrap();
    home.stage_harness_auth().unwrap();
    assert_eq!(
        std::fs::read_to_string(home.path().join("agent-auth")).unwrap(),
        "staged"
    );
    let mut check_home = HomeGuard::for_check(&dir).unwrap();
    check_home.stage_harness_auth().unwrap();
    for (confine, child_home) in [
        (confine_harness as fn(&mut Command, &Path), home.path()),
        (confine_check, check_home.path()),
    ] {
        let mut cmd = Command::new("sh");
        let script = [
            "env",
            "test \"$(cat \"$HOME/agent-auth\")\" = staged",
            "test ! -e \"$HOME/.git-credentials\"",
            "test ! -e \"$HOME/.gitconfig\"",
            "test -z \"$(git config --get-regexp '^credential')\"",
            "test ! -e \"$HOME/.ssh/id_test\"",
            "test ! -e \"$HOME/.config/gh/hosts.yml\"",
            "git init -q",
            "test -z \"$(git remote)\"",
            "! git push",
        ]
        .join(" && ");
        cmd.current_dir(&dir).args(["-c", &script]);
        confine(&mut cmd, child_home);
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
    match old_home {
        Some(v) => std::env::set_var("HOME", v),
        None => std::env::remove_var("HOME"),
    }
}
