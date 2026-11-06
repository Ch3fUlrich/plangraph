use super::*;

struct Restore(Vec<(&'static str, Option<std::ffi::OsString>)>);
impl Restore {
    fn set(&mut self, name: &'static str, value: &std::ffi::OsStr) {
        self.0.push((name, std::env::var_os(name)));
        std::env::set_var(name, value);
    }
}
impl Drop for Restore {
    fn drop(&mut self) {
        for (name, value) in self.0.drain(..).rev() {
            match value {
                Some(v) => std::env::set_var(name, v),
                None => std::env::remove_var(name),
            }
        }
    }
}

#[test]
#[serial_test::serial(env)]
fn environment_restores_on_panic() {
    let name = "PG_PILOT_RESTORE_TEST";
    let before = std::env::var_os(name);
    let panic = std::panic::catch_unwind(|| {
        let mut guard = Restore(vec![]);
        guard.set(name, std::ffi::OsStr::new("changed"));
        assert_eq!(std::env::var(name).unwrap(), "changed");
        panic!("exercise restoration");
    });
    assert!(panic.is_err());
    assert_eq!(std::env::var_os(name), before);
}

#[test]
#[serial_test::serial(env)]
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
    let lookup = "printf 'protocol=https\nhost=example.invalid\n\n' | GIT_TERMINAL_PROMPT=0 git -c credential.interactive=false credential fill";
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
    let mut restore = Restore(vec![]);
    restore.set(HARNESS_AUTH_DIR_ENV, source.as_os_str());
    restore.set("HOME", source.as_os_str());
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
        restore.set(name, std::ffi::OsStr::new("pilot-parent"));
    }
    let dir = root.path().join("work");
    std::fs::create_dir(&dir).unwrap();
    for (confine, mut home) in [
        (
            confine_harness as fn(&mut Command, &Path),
            HomeGuard::for_dir(&dir).unwrap(),
        ),
        (confine_check, HomeGuard::for_check(&dir).unwrap()),
    ] {
        home.stage_harness_auth().unwrap();
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
            .env("LC_ALL", "C")
            .env("GIT_TERMINAL_PROMPT", "0")
            .env("GIT_CONFIG_NOSYSTEM", "1");
        confine(&mut cmd, home.path());
        let out = cmd.output().unwrap();
        assert!(!out.status.success());
        let stderr = String::from_utf8_lossy(&out.stderr);
        assert!(
            stderr.contains("fatal: unable to get password from user"),
            "{stderr}"
        );
        assert!(!String::from_utf8_lossy(&out.stdout).contains("fixture"));
        assert!(!String::from_utf8_lossy(&out.stderr).contains("fixture"));
        for name in names {
            assert_eq!(std::env::var(name).unwrap(), "pilot-parent");
            assert!(!String::from_utf8_lossy(&out.stdout)
                .lines()
                .any(|l| l.starts_with(&format!("{name}="))));
        }
    }
}
