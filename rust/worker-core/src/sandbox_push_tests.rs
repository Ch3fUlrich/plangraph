use super::*;

#[test]
fn pilot_credentials_absent_with_parent_positive_control() {
    let root = tempfile::tempdir().unwrap();
    let source = root.path().join("source");
    std::fs::create_dir_all(source.join("sub")).unwrap();
    let blocked = [
        ".git-credentials",
        ".gitconfig",
        ".ssh",
        ".netrc",
        ".npmrc",
        ".pypirc",
        ".aws",
        ".docker",
        ".gnupg",
        "hosts.yml",
        "credentials",
        ".config",
    ];
    for name in blocked {
        for base in [&source, &source.join("sub")] {
            std::fs::write(base.join(name), "parent credential").unwrap();
            assert!(base.join(name).is_file());
        }
    }
    for name in ["agent-auth", "sub/agent-auth"] {
        std::fs::write(source.join(name), "staged").unwrap();
    }
    std::fs::write(source.join(".gitconfig"), "[credential]\n helper = store\n").unwrap();
    std::fs::write(
        source.join(".git-credentials"),
        ["https://pilot:", "fixture@example.invalid\n"].concat(),
    )
    .unwrap();
    // Supply the public username so every Git version fails at the missing
    // password, while the parent positive control still requires its secret.
    let lookup = "printf 'protocol=https\nhost=example.invalid\nusername=pilot\n\n' | GIT_TERMINAL_PROMPT=0 git -c credential.interactive=false credential fill";
    let parent = Command::new("sh")
        .args(["-c", lookup])
        .env_clear()
        .env("PATH", std::env::var_os("PATH").unwrap())
        .env("HOME", &source)
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("LC_ALL", "C")
        .output()
        .unwrap();
    assert!(
        parent.status.success(),
        "{}",
        String::from_utf8_lossy(&parent.stderr)
    );
    assert!(String::from_utf8_lossy(&parent.stdout).contains("password=fixture"));
    let mut parent_env: Vec<_> = std::env::vars_os().collect();
    parent_env.push(("HOME".into(), source.clone().into_os_string()));
    let names = [
        "GIT_ASKPASS",
        "GIT_SSH_COMMAND",
        "SSH_AUTH_SOCK",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "GITLAB_TOKEN",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_COUNT",
    ];
    for name in names {
        parent_env.push((name.into(), "pilot-parent".into()));
    }
    let dir = root.path().join("work");
    std::fs::create_dir(&dir).unwrap();
    for (confine, mut home) in [
        (
            is_passed as fn(&str) -> bool,
            HomeGuard::for_dir(&dir).unwrap(),
        ),
        (is_passed_check, HomeGuard::for_check(&dir).unwrap()),
    ] {
        home.stage_harness_auth_from(Some(&source)).unwrap();
        for name in blocked {
            for base in [home.path().to_path_buf(), home.path().join("sub")] {
                assert!(
                    !base.join(name).exists(),
                    "{} leaked",
                    base.join(name).display()
                );
            }
        }
        for name in ["agent-auth", "sub/agent-auth"] {
            assert_eq!(
                std::fs::read_to_string(home.path().join(name)).unwrap(),
                "staged"
            );
        }
        let mut cmd = Command::new("sh");
        cmd.current_dir(&dir)
            .args(["-c", &format!("env; {lookup}")])
            .env("GIT_TERMINAL_PROMPT", "0")
            .env("GIT_CONFIG_NOSYSTEM", "1");
        confine_with(&mut cmd, home.path(), confine, parent_env.clone());
        // Pin Git's diagnostic language after confinement for the exact match below.
        cmd.env("LC_ALL", "C");
        let out = cmd.output().unwrap();
        assert!(!out.status.success());
        let stderr = String::from_utf8_lossy(&out.stderr);
        assert!(
            stderr.contains("fatal: unable to get password from user")
                || ((stderr.contains("fatal: could not read Password for")
                    || stderr
                        .contains("fatal: could not read Username for 'https://example.invalid'"))
                    && stderr.contains("terminal prompts disabled")),
            "{stderr}"
        );
        assert!(!String::from_utf8_lossy(&out.stdout).contains("fixture"));
        assert!(!String::from_utf8_lossy(&out.stderr).contains("fixture"));
        for name in names {
            assert!(parent_env
                .iter()
                .any(|(k, v)| k == name && v == "pilot-parent"));
            assert!(!String::from_utf8_lossy(&out.stdout)
                .lines()
                .any(|l| l.starts_with(&format!("{name}="))));
        }
    }
}
