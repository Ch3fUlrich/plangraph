#[test]
fn json_snapshot_is_complete_and_text_cuts_are_marked() {
    let snapshot=serde_json::json!({"items":(0..1000).map(|n|serde_json::json!({"key":format!("p:ITEM-{n}"),"state":"open"})).collect::<Vec<_>>()}).to_string()+"\n";
    let output = pg_fleet::keeper_wrapper::output(&snapshot, true);
    assert_eq!(output, snapshot);
    assert_eq!(
        serde_json::from_str::<serde_json::Value>(&output).unwrap()["items"]
            .as_array()
            .unwrap()
            .len(),
        1000
    );
    assert!(pg_fleet::keeper_wrapper::output(&snapshot, false)
        .ends_with("[truncated; use --json for full output]\n"));
    assert_eq!(
        pg_fleet::keeper_wrapper::output("short\n", false),
        "short\n"
    );
}

#[test]
fn cli_json_snapshot_parses_past_the_old_limit() {
    let d = tempfile::tempdir().unwrap();
    let data = d.path().join("snapshot.json");
    let text=serde_json::json!({"items":vec![serde_json::json!({"key":"p:ITEM","title":"x".repeat(100)});100]}).to_string();
    std::fs::write(&data, &text).unwrap();
    #[cfg(windows)]
    let fake = d.path().join("pg.cmd");
    #[cfg(unix)]
    let fake = d.path().join("pg");
    #[cfg(windows)]
    std::fs::write(&fake, "@echo off\r\ntype \"%KC_TEST_JSON%\"\r\n").unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::write(&fake, "#!/bin/sh\n/bin/cat \"$KC_TEST_JSON\"\n").unwrap();
        std::fs::set_permissions(&fake, std::fs::Permissions::from_mode(0o700)).unwrap();
    }
    let out = std::process::Command::new(env!("CARGO_BIN_EXE_kc"))
        .args(["--json", "snapshot"])
        .env("HOME", d.path())
        .env("KC_PROGRAM", &fake)
        .env("KC_TEST_JSON", &data)
        .output()
        .unwrap();
    assert!(out.status.success());
    assert_eq!(out.stdout, text.as_bytes());
    assert!(serde_json::from_slice::<serde_json::Value>(&out.stdout).is_ok());
}
