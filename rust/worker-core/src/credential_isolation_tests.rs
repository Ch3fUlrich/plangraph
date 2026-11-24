use super::*;
use serde_json::json;
use std::fs;

fn fixture_deny() -> Vec<PathBuf> {
    deny_read_paths(Path::new("/fixture/user"), None)
}

#[test]
fn the_deny_list_names_the_plangraph_config_directory_and_the_ssh_directory() {
    let deny = fixture_deny();
    for expected in [
        "/fixture/user/.config/plangraph",
        "/fixture/user/.ssh",
        "/fixture/user/.aws",
        "/fixture/user/.git-credentials",
        // The keeper daemon config of deploy/systemd/plangraph-keeper.service holds the DSN.
        "/fixture/user/.local/share/plangraph/keeper.json",
    ] {
        assert!(
            deny.contains(&PathBuf::from(expected)),
            "{expected}: {deny:?}"
        );
    }
}

#[test]
fn a_relocated_config_home_is_denied_as_well_as_the_default() {
    let deny = deny_read_paths(Path::new("/fixture/user"), Some(Path::new("/fixture/xdg")));
    assert!(deny.contains(&PathBuf::from("/fixture/xdg/plangraph")));
    assert!(deny.contains(&PathBuf::from("/fixture/user/.config/plangraph")));
    // A relative or equal config home adds nothing and never a duplicate.
    let same = deny_read_paths(
        Path::new("/fixture/user"),
        Some(Path::new("/fixture/user/.config")),
    );
    assert_eq!(same, fixture_deny());
    assert_eq!(
        deny_read_paths(Path::new("/fixture/user"), Some(Path::new("relative"))),
        fixture_deny()
    );
}

#[test]
fn the_profile_denies_every_listed_path_and_keeps_the_writer_roots_and_network() {
    let args = profile_args(&[PathBuf::from("/fixture/target")], &fixture_deny()).unwrap();
    let joined = args.join(" ");
    assert_eq!(args[0], "-c");
    assert!(
        joined.contains(&format!("default_permissions=\"{PROFILE}\"")),
        "{joined}"
    );
    assert!(joined.contains("\"/fixture/target\"=\"write\""), "{joined}");
    assert!(joined.contains("\":root\"=\"read\""), "{joined}");
    assert!(
        joined.contains(&format!("permissions.{PROFILE}.network.enabled=true")),
        "{joined}"
    );
    for path in fixture_deny() {
        let entry = format!("\"{}\"=\"none\"", path.display());
        assert!(joined.contains(&entry), "{entry} missing: {joined}");
    }
    assert!(!joined.contains("danger-full-access"), "{joined}");
}

#[test]
fn a_card_that_asks_for_danger_full_access_is_refused_with_a_named_reason() {
    let reason = refuse_card_sandbox(&json!({"sandbox": "danger-full-access"})).unwrap_err();
    assert!(reason.starts_with(REFUSED), "{reason}");
    assert!(reason.contains("danger-full-access"), "{reason}");
    for other in [json!({"sandbox": "read-only"}), json!({"sandbox": 1})] {
        assert!(refuse_card_sandbox(&other)
            .unwrap_err()
            .starts_with(REFUSED));
    }
    for fine in [
        json!({}),
        json!({"sandbox": null}),
        json!({"sandbox": "workspace-write"}),
    ] {
        refuse_card_sandbox(&fine).unwrap();
    }
}

#[test]
fn harness_api_keys_are_excluded_from_the_commands_the_agent_runs() {
    let args = env_policy_args().join(" ");
    assert!(
        args.contains("shell_environment_policy.exclude=[\"OPENAI_*\"]"),
        "{args}"
    );
    assert!(args.contains("ignore_default_excludes=false"), "{args}");
    let writer = crate::host_readiness::writer_sandbox_args(&[], &fixture_deny())
        .unwrap()
        .join(" ");
    assert!(
        writer.contains("shell_environment_policy.exclude="),
        "{writer}"
    );
}

#[test]
fn the_plangraph_config_directories_follow_the_config_home() {
    let dirs = plangraph_config_dirs(Path::new("/fixture/user"), Some(Path::new("/fixture/xdg")));
    assert_eq!(
        dirs,
        [
            PathBuf::from("/fixture/user/.config/plangraph"),
            PathBuf::from("/fixture/xdg/plangraph")
        ]
    );
}

/// Real Codex sandbox with fixture files only: the fixture secret under a fixture config
/// directory cannot be read, a file next to it in the workspace can.
#[cfg(target_os = "linux")]
#[test]
#[ignore = "live host check requiring a working Codex sandbox"]
fn a_sandboxed_fixture_run_cannot_read_a_fixture_secret_under_the_config_directory() {
    let dir = tempfile::tempdir_in(env!("CARGO_MANIFEST_DIR")).unwrap();
    let home = fs::canonicalize(dir.path()).unwrap();
    let config = home.join(".config/plangraph");
    fs::create_dir_all(&config).unwrap();
    fs::write(config.join("keeper.dsn"), "fixture-secret").unwrap();
    let keeper = home.join(".local/share/plangraph/keeper.json");
    fs::create_dir_all(keeper.parent().unwrap()).unwrap();
    fs::write(&keeper, "fixture-secret").unwrap();
    let workspace = home.join("workspace");
    fs::create_dir(&workspace).unwrap();
    fs::write(workspace.join("plain"), "fixture-plain").unwrap();
    let deny = deny_read_paths(&home, None);
    let out = std::process::Command::new("codex")
        .arg("sandbox")
        .args(crate::host_readiness::writer_sandbox_args(&[], &deny).unwrap())
        .args([
            "--",
            "sh",
            "-c",
            "cat plain; cat \"$1\" || echo secret-denied; cat \"$2\" || echo keeper-denied",
            "fixture",
        ])
        .arg(config.join("keeper.dsn"))
        .arg(&keeper)
        .current_dir(&workspace)
        .output()
        .unwrap();
    let text = String::from_utf8_lossy(&out.stdout);
    assert!(
        text.contains("fixture-plain")
            && text.contains("secret-denied")
            && text.contains("keeper-denied"),
        "{text}"
    );
    assert!(!text.contains("fixture-secret"), "{text}");
}

#[test]
fn the_child_open_verdicts_name_denied_readable_and_absent_without_reading() {
    let dir = tempfile::tempdir().unwrap();
    let file = dir.path().join("fixture.dsn");
    fs::write(&file, "fixture-secret").unwrap();
    assert_eq!(child_open(&file), "readable");
    assert_eq!(child_open(dir.path()), "readable");
    assert_eq!(child_open(&dir.path().join("missing")), "absent");
}

#[test]
fn the_session_guard_list_adds_the_codex_auth_file_to_the_sandbox_list() {
    let home = Path::new("/fixture/user");
    let guard = session_guard_paths(home, None);
    assert!(guard.contains(&PathBuf::from("/fixture/user/.codex/auth.json")));
    assert!(deny_read_paths(home, None)
        .iter()
        .all(|path| guard.contains(path)));
    assert!(!deny_read_paths(home, None).contains(&PathBuf::from("/fixture/user/.codex/auth.json")));
}
