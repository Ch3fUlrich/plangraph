#![cfg(unix)]
use std::process::Command;
#[test]
fn cli_names_file_and_line_and_accepts_bare_tools() {
    let root = tempfile::tempdir().unwrap();
    let file = root.path().join("settings.json");
    std::fs::write(
        &file,
        "{\n\"permissions\": {\n\"deny\": [\"mcp__terminal__run(*)\"]\n}}\n",
    )
    .unwrap();
    let result = Command::new(env!("CARGO_BIN_EXE_pg-settings-lint"))
        .arg(&file)
        .output()
        .unwrap();
    assert!(!result.status.success());
    assert!(String::from_utf8_lossy(&result.stderr).contains(&format!("{}:3:", file.display())));
    std::fs::write(&file, r#"{"permissions":{"allow":["mcp__terminal__run"]}}"#).unwrap();
    assert!(Command::new(env!("CARGO_BIN_EXE_pg-settings-lint"))
        .arg(file)
        .status()
        .unwrap()
        .success());
}
