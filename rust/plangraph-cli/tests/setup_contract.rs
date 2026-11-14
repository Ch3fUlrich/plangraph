use plangraph_cli::{
    atomic, env,
    setup::{self, Mode},
};
use std::fs;
use std::path::Path;
fn manifest(format: &str, content: &str) -> String {
    format!("[[files]]\ncomponent='test'\ntarget='settings.json'\nformat='{format}'\ncontent='''{content}'''\n")
}
fn run(root: &Path, text: &str, mode: Mode) -> (i32, Vec<String>) {
    setup::run(root, text, mode)
}
#[test]
fn apply_is_idempotent_check_and_dry_run_write_nothing() {
    let root = tempfile::tempdir().unwrap();
    let text = manifest("text", "owned=true");
    assert_eq!(run(root.path(), &text, Mode::DryRun).0, 0);
    assert_eq!(fs::read_dir(root.path()).unwrap().count(), 0);
    assert_eq!(run(root.path(), &text, Mode::Check).0, 1);
    assert_eq!(fs::read_dir(root.path()).unwrap().count(), 0);
    assert_eq!(run(root.path(), &text, Mode::Apply).0, 0);
    assert!(run(root.path(), &text, Mode::Apply).1[0].contains("skipped"));
    assert_eq!(run(root.path(), &text, Mode::Check).0, 0);
    assert_eq!(fs::read_dir(root.path()).unwrap().count(), 2);
    fs::write(root.path().join("settings.json"), "owned=false").unwrap();
    assert_eq!(run(root.path(), &text, Mode::Check).0, 1);
}
#[test]
fn refuses_unparsable_and_unsafe_targets_without_backup() {
    let root = tempfile::tempdir().unwrap();
    let file = root.path().join("settings.json");
    fs::write(&file, "{broken").unwrap();
    assert_eq!(run(root.path(), &manifest("env", "A=1"), Mode::Apply).0, 2);
    assert_eq!(fs::read_to_string(file).unwrap(), "{broken");
    assert_eq!(fs::read_dir(root.path()).unwrap().count(), 2);
    for target in ["../escape", "/absolute", "a/../../escape"] {
        let text = manifest("env", "A=1").replace("settings.json", target);
        assert_eq!(run(root.path(), &text, Mode::Apply).0, 2);
    }
}
#[test]
fn backups_only_on_change_retains_three_and_lock_refuses_competitor() {
    let root = tempfile::tempdir().unwrap();
    let target = root.path().join("settings.json");
    for n in 0..6 {
        assert_eq!(
            run(
                root.path(),
                &manifest("text", &format!("n={n}")),
                Mode::Apply
            )
            .0,
            0
        );
    }
    let backups: Vec<_> = fs::read_dir(root.path())
        .unwrap()
        .map(Result::unwrap)
        .filter(|e| e.file_name().to_string_lossy().contains("backup"))
        .collect();
    assert_eq!(backups.len(), 3);
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        for file in backups {
            assert_eq!(file.metadata().unwrap().permissions().mode() & 0o777, 0o600);
        }
    }
    let _lock = atomic::lock(&target).unwrap();
    assert_eq!(run(root.path(), &manifest("env", "A=1"), Mode::Apply).0, 2);
}
#[test]
fn env_precedence_unknown_values_and_secret_refusal() {
    let root = tempfile::tempdir().unwrap();
    let file = root.path().join("env");
    fs::write(
        &file,
        "PG_URL=file\nOTHER='keep'\nPG_TOKEN_FILE=/private/token\n",
    )
    .unwrap();
    let values = env::load(&file, [("PG_URL".into(), "process".into())]).unwrap();
    assert_eq!(values["PG_URL"], "process");
    assert_eq!(values["OTHER"], "keep");
    assert_eq!(values["PG_LEASE_S"], "7200");
    assert!(env::parse("API_KEY=do-not-print")
        .unwrap_err()
        .contains("inline secret"));
    assert!(env::parse("A=1\nA=2").is_err());
}
#[test]
fn embedded_manifest_and_partial_failure() {
    let root = tempfile::tempdir().unwrap();
    assert_eq!(run(root.path(), setup::MANIFEST, Mode::Apply).0, 0);
    assert!(run(root.path(), setup::MANIFEST, Mode::Apply)
        .1
        .iter()
        .all(|s| s.contains("skipped")));
    let text = manifest("env", "A=1")
        + "\n[[files]]\ncomponent='bad'\ntarget='../escape'\nformat='text'\ncontent='x'\n";
    assert_eq!(run(root.path(), &text, Mode::Apply).0, 3);
}
#[cfg(unix)]
#[test]
fn symlink_ancestor_is_refused() {
    let root = tempfile::tempdir().unwrap();
    let outside = tempfile::tempdir().unwrap();
    std::os::unix::fs::symlink(outside.path(), root.path().join("link")).unwrap();
    let text = manifest("env", "A=1").replace("settings.json", "link/settings.json");
    assert_eq!(run(root.path(), &text, Mode::Apply).0, 2);
    assert_eq!(fs::read_dir(outside.path()).unwrap().count(), 0);
}

#[test]
fn inline_pat_and_key_credentials_are_refused_without_leaking_values() {
    for key in ["GITHUB_PAT", "SSH_KEY", "PRIVATE_KEY", "KEY", "PAT"] {
        let result = env::parse(&format!("{key}=sensitive-fixture"));
        assert!(result.is_err(), "accepted inline {key}");
        assert!(!result.unwrap_err().contains("sensitive-fixture"));
        assert!(env::parse(&format!("{key}_FILE=/private/key")).is_ok());
    }
}

#[test]
fn setup_documentation_does_not_claim_windows_test_execution() {
    let documentation = include_str!("../../../docs/setup.md");
    assert!(!documentation.contains("contract tests run on Linux and Windows CI"));
    assert!(documentation.contains(
        "CI builds and tests this crate on Linux only; Windows CI does not compile or run it."
    ));
}

#[test]
fn env_setup_preserves_existing_values_comments_and_unknown_keys() {
    let root = tempfile::tempdir().unwrap();
    let target = root.path().join("settings.json");
    fs::write(&target, "# keep\nPG_URL='custom'\nOTHER=keep").unwrap();
    let text = manifest("env", "PG_URL=default\nPG_LEASE_S=7200\n");
    assert_eq!(run(root.path(), &text, Mode::Apply).0, 0);
    assert_eq!(
        fs::read_to_string(&target).unwrap(),
        "# keep\nPG_URL='custom'\nOTHER=keep\nPG_LEASE_S=7200\n"
    );
    assert_eq!(run(root.path(), &text, Mode::Check).0, 0);
}

#[test]
fn malformed_manifests_unsupported_formats_and_directory_targets_are_refused() {
    let root = tempfile::tempdir().unwrap();
    assert_eq!(run(root.path(), "invalid TOML", Mode::Apply).0, 2);
    assert_eq!(
        run(
            root.path(),
            &(manifest("text", "x") + "unknown=true\n"),
            Mode::Apply
        )
        .0,
        2
    );
    for format in ["json", "hooks", "unknown"] {
        assert_eq!(run(root.path(), &manifest(format, "{}"), Mode::Apply).0, 2);
        assert!(!root.path().join("settings.json").exists());
    }
    fs::create_dir(root.path().join("settings.json")).unwrap();
    assert_eq!(run(root.path(), &manifest("text", "x"), Mode::Apply).0, 2);
}

#[test]
fn secret_manifest_default_is_refused_even_when_existing_key_would_win() {
    let root = tempfile::tempdir().unwrap();
    let text = manifest("env", "SERVICE_KEY=sensitive-fixture");
    let result = run(root.path(), &text, Mode::Apply);
    assert_eq!(result.0, 2);
    assert!(!result.1.join("\n").contains("sensitive-fixture"));
    assert!(!root.path().join("settings.json").exists());
}

#[cfg(unix)]
#[test]
fn symlink_above_home_is_allowed_but_home_and_descendants_must_not_be_links() {
    let root = tempfile::tempdir().unwrap();
    let real = root.path().join("real");
    fs::create_dir_all(real.join("home")).unwrap();
    let alias = root.path().join("alias");
    std::os::unix::fs::symlink(&real, &alias).unwrap();
    let home = alias.join("home");
    let text = manifest("text", "fixture");
    assert_eq!(run(&home, &text, Mode::Apply).0, 0);
    assert_eq!(run(&home, &text, Mode::Check).0, 0);
    assert_eq!(
        fs::read_to_string(real.join("home/settings.json")).unwrap(),
        "fixture"
    );

    let outside = root.path().join("outside");
    fs::create_dir(&outside).unwrap();
    std::os::unix::fs::symlink(&outside, home.join("link")).unwrap();
    let escaped = text.replace("settings.json", "link/settings.json");
    assert_eq!(run(&home, &escaped, Mode::Apply).0, 2);
    assert_eq!(fs::read_dir(&outside).unwrap().count(), 0);
    assert_eq!(run(&alias, &text, Mode::Apply).0, 2);
}
