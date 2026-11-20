use super::*;
use std::ffi::OsStr;

#[test]
fn missing_key_file_produces_one_alert_and_resets_when_present() {
    let dir = tempfile::tempdir().unwrap();
    let state = dir.path().join("state");
    let key = dir.path().join("autoos/ai-stack/manage.key");

    let first = check(&state, &key).unwrap();
    assert_eq!(first["status"], "alert");
    assert_eq!(first["emitted"], true);
    assert_eq!(first["alert"], "missing_manage_key");
    assert_eq!(first["count"], 1);
    let key_str = key.to_string_lossy().to_string();
    assert!(!first.to_string().contains(&key_str));
    assert!(first.get("key_path").is_none());
    let persisted = fs::read_to_string(state.join("alerts/manage-key.json")).unwrap();
    assert!(!persisted.contains(&key_str));

    let second = check(&state, &key).unwrap();
    assert_eq!(second["status"], "alert");
    assert_eq!(second["emitted"], false);
    assert_eq!(second["alert"], "missing_manage_key");
    assert_eq!(second["count"], 1);

    fs::create_dir_all(key.parent().unwrap()).unwrap();
    fs::write(&key, "fixture-placeholder\n").unwrap();
    let recovered = check(&state, &key).unwrap();
    assert_eq!(recovered["status"], "ok");
    assert_eq!(recovered["emitted"], false);
    assert!(recovered["alert"].is_null());
    assert_eq!(recovered["count"], 0);

    fs::remove_file(&key).unwrap();
    let relost = check(&state, &key).unwrap();
    assert_eq!(relost["status"], "alert");
    assert_eq!(relost["emitted"], true);
    assert_eq!(relost["count"], 1);
}

#[test]
fn empty_key_file_is_treated_as_missing() {
    let dir = tempfile::tempdir().unwrap();
    let state = dir.path().join("state");
    let key = dir.path().join("manage.key");
    fs::write(&key, "").unwrap();
    assert!(!key_present(&key));
    let out = check(&state, &key).unwrap();
    assert_eq!(out["status"], "alert");
    assert_eq!(out["emitted"], true);
}

#[test]
fn resolve_key_path_precedence_and_cli_execution() {
    let custom = resolve_key_path(Some("/tmp/custom/manage.key"), |_| None).unwrap();
    assert_eq!(custom, PathBuf::from("/tmp/custom/manage.key"));

    let stack_cfg = resolve_key_path(None, |k| {
        (k == "AUTOOS_AI_STACK_CONFIG").then(|| OsStr::new("/tmp/stack").to_os_string())
    })
    .unwrap();
    assert_eq!(stack_cfg, PathBuf::from("/tmp/stack/manage.key"));

    let dir = tempfile::tempdir().unwrap();
    let state = dir.path().join("state");
    let key = dir.path().join("manage.key");
    let mut out = Vec::new();
    execute(
        vec![
            "--state".into(),
            state.to_string_lossy().into(),
            "--key-file".into(),
            key.to_string_lossy().into(),
            "--json".into(),
        ],
        &mut out,
    )
    .unwrap();
    let parsed: Value = serde_json::from_slice(&out).unwrap();
    assert_eq!(parsed["status"], "alert");
    assert_eq!(parsed["emitted"], true);

    let mut text_out = Vec::new();
    execute(
        vec![
            "--state".into(),
            state.to_string_lossy().into(),
            "--key-file".into(),
            key.to_string_lossy().into(),
        ],
        &mut text_out,
    )
    .unwrap();
    assert!(!String::from_utf8_lossy(&out).contains(&*key.to_string_lossy()));
    assert!(
        text_out.is_empty(),
        "second check must not duplicate text alert"
    );
}

#[test]
fn text_alert_names_no_path() {
    let dir = tempfile::tempdir().unwrap();
    let state = dir.path().join("state");
    let key = dir.path().join("manage.key");
    let mut out = Vec::new();
    execute(
        vec![
            "--state".into(),
            state.to_string_lossy().into(),
            "--key-file".into(),
            key.to_string_lossy().into(),
        ],
        &mut out,
    )
    .unwrap();
    let text = String::from_utf8(out).unwrap();
    assert_eq!(text.trim(), "ALERT missing_manage_key");
    assert!(!text.contains(&*key.to_string_lossy()));
}
