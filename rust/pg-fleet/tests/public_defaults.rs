#[allow(dead_code)] // Shared git helper is unused by these command-isolation tests.
mod support;
use support::isolated;

#[test]
fn numbered_router_roles_keep_role_matching() {
    for name in ["routing-01", "routing-0001", "routing-123456"] {
        assert_eq!(pg_fleet::digest_text::role(name), "routing");
    }
    assert_eq!(pg_fleet::digest_text::role("routing-x"), "routing-x");
    assert_eq!(pg_fleet::digest_text::role("build-L1-0001"), "build-L1");
}

#[test]
fn data_directory_uses_public_default_and_explicit_override() {
    let d = tempfile::tempdir().unwrap();
    let home = d.path().join("home");
    let default = home.join(".local/share/plangraph/fleet");
    let custom = d.path().join("custom");
    for data in [&default, &custom] {
        std::fs::create_dir_all(data).unwrap();
        std::fs::write(data.join("QUESTIONS.md"), "| 0 | x | TASK |\n").unwrap();
    }
    for override_ in [
        None,
        Some(std::ffi::OsStr::new("")),
        Some(custom.as_os_str()),
    ] {
        let mut c = isolated(env!("CARGO_BIN_EXE_question-count"), &home);
        c.env_remove("RESUME_DIGEST_HOME");
        if let Some(value) = override_ {
            c.env("RESUME_DIGEST_HOME", value);
        }
        let out = c.output().unwrap();
        assert!(
            out.status.success(),
            "{}",
            String::from_utf8_lossy(&out.stderr)
        );
        assert!(String::from_utf8_lossy(&out.stdout).starts_with("operator queue: 1 open"));
    }
}

#[test]
fn merge_requires_repository_configuration_before_remote_calls() {
    let d = tempfile::tempdir().unwrap();
    let wrapper = d.path().join("wrapper.sh");
    std::fs::write(&wrapper, "exit 99\n").unwrap();
    for value in [None, Some("")] {
        let mut c = isolated(env!("CARGO_BIN_EXE_merge-part"), &d.path().join("home"));
        c.args(["1", "aaaaaaaa", "p:ITEM", "D-1", "subject", "body"])
            .env("PG_PGK", &wrapper)
            .env("PG_CI_EVIDENCE", &wrapper)
            .env_remove("PG_REPO")
            .env("PATH", d.path());
        if let Some(value) = value {
            c.env("PG_REPO", value);
        }
        let out = c.output().unwrap();
        assert_eq!(out.status.code(), Some(1));
        assert_eq!(
            out.stdout,
            b"STOP: PG_REPO must name the target repository\n"
        );
        assert!(out.stderr.is_empty());
    }
}
