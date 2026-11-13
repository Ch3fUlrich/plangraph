use plangraph_cli::{
    atomic, env,
    setup::{self, Mode},
};
use serde_json::json;
use std::fs;
use std::path::Path;
fn manifest(format: &str, content: &str) -> String {
    format!("[[files]]\ncomponent='test'\ntarget='settings.json'\nformat='{format}'\ncontent='''{content}'''\n")
}
fn run(root: &Path, text: &str, mode: Mode) -> (i32, Vec<String>) {
    setup::run(root, Path::new("/bin/plangraph"), text, mode)
}
#[test]
fn apply_is_idempotent_check_and_dry_run_write_nothing() {
    let root = tempfile::tempdir().unwrap();
    let text = manifest("json", r#"{"owned":true}"#);
    assert_eq!(run(root.path(), &text, Mode::DryRun).0, 0);
    assert_eq!(fs::read_dir(root.path()).unwrap().count(), 0);
    assert_eq!(run(root.path(), &text, Mode::Check).0, 1);
    assert_eq!(fs::read_dir(root.path()).unwrap().count(), 0);
    assert_eq!(run(root.path(), &text, Mode::Apply).0, 0);
    assert!(run(root.path(), &text, Mode::Apply).1[0].contains("skipped"));
    assert_eq!(run(root.path(), &text, Mode::Check).0, 0);
    assert_eq!(fs::read_dir(root.path()).unwrap().count(), 2);
    fs::write(root.path().join("settings.json"), "{\"owned\":false}").unwrap();
    assert_eq!(run(root.path(), &text, Mode::Check).0, 1);
}
#[test]
fn refuses_unparsable_and_unsafe_targets_without_backup() {
    let root = tempfile::tempdir().unwrap();
    let file = root.path().join("settings.json");
    fs::write(&file, "{broken").unwrap();
    assert_eq!(run(root.path(), &manifest("json", "{}"), Mode::Apply).0, 2);
    assert_eq!(fs::read_to_string(file).unwrap(), "{broken");
    assert_eq!(fs::read_dir(root.path()).unwrap().count(), 2);
    for target in ["../escape", "/absolute", "a/../../escape"] {
        let text = manifest("json", "{}").replace("settings.json", target);
        assert_eq!(run(root.path(), &text, Mode::Apply).0, 2);
    }
}
#[test]
fn preserves_unknown_entries_replaces_superseded_hook_in_place() {
    let root = tempfile::tempdir().unwrap();
    let target = root.path().join("settings.json");
    let original = json!({"theme":"custom", "hooks":{"Stop":[{"matcher":"mine", "extra":42,
        "hooks":[{"type":"command","command":"python3 /old/tools/fleet_report.py", "timeout":12},
                 {"type":"command","command":"user-command"}, {"type":"prompt","prompt":"keep me"}]}]}});
    fs::write(&target, original.to_string()).unwrap();
    let text = manifest(
        "hooks",
        r#"{"Stop":[{"hooks":[{"type":"command","command":"plangraph hook report"}]}]}"#,
    ) + "superseded=['python3 */tools/fleet_report.py']\n";
    assert_eq!(run(root.path(), &text, Mode::Apply).0, 0);
    let result: serde_json::Value = serde_json::from_slice(&fs::read(&target).unwrap()).unwrap();
    assert_eq!(result["theme"], "custom");
    assert_eq!(result.pointer("/hooks/Stop/0/extra"), Some(&json!(42)));
    assert_eq!(
        result.pointer("/hooks/Stop/0/hooks/0/timeout"),
        Some(&json!(12))
    );
    assert_eq!(
        result.pointer("/hooks/Stop/0/hooks/1/command"),
        Some(&json!("user-command"))
    );
    assert!(run(root.path(), &text, Mode::Apply).1[0].contains("skipped"));
}
#[test]
fn backups_only_on_change_retains_three_and_lock_refuses_competitor() {
    let root = tempfile::tempdir().unwrap();
    let target = root.path().join("settings.json");
    for n in 0..6 {
        assert_eq!(
            run(
                root.path(),
                &manifest("json", &format!("{{\"n\":{n}}}")),
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
    assert_eq!(run(root.path(), &manifest("json", "{}"), Mode::Apply).0, 2);
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
    let text = manifest("json", "{}")
        + "\n[[files]]\ncomponent='bad'\ntarget='../escape'\nformat='text'\ncontent='x'\n";
    assert_eq!(run(root.path(), &text, Mode::Apply).0, 3);
}
#[cfg(unix)]
#[test]
fn symlink_ancestor_is_refused() {
    let root = tempfile::tempdir().unwrap();
    let outside = tempfile::tempdir().unwrap();
    std::os::unix::fs::symlink(outside.path(), root.path().join("link")).unwrap();
    let text = manifest("json", "{}").replace("settings.json", "link/settings.json");
    assert_eq!(run(root.path(), &text, Mode::Apply).0, 2);
    assert_eq!(fs::read_dir(outside.path()).unwrap().count(), 0);
}

#[test]
fn superseded_patterns_cannot_replace_two_distinct_commands() {
    let mut target = json!({});
    let desired = json!({"Stop":[{"hooks":[{"command":"one"}]},{"hooks":[{"command":"two"}]}]});
    assert!(plangraph_cli::merge::hooks(&mut target, &desired, &["legacy*".into()]).is_err());
}
#[cfg(target_os = "linux")]
#[test]
fn systemd_binary_does_not_expand_dollars_or_specifiers() {
    let root = tempfile::tempdir().unwrap();
    let binary = Path::new("/tmp/binary ${UNKNOWN}%x/plangraph");
    assert_eq!(
        setup::run(root.path(), binary, setup::MANIFEST, Mode::Apply).0,
        0
    );
    let service = fs::read_to_string(
        root.path()
            .join(".config/systemd/user/taildrop-sort.service"),
    )
    .unwrap();
    assert!(service.contains("binary $${UNKNOWN}%%x/plangraph"));
}
