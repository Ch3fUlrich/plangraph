use std::{fs, process::Command};

fn check(source: &str) -> std::process::Output {
    let dir = tempfile::tempdir().unwrap();
    fs::create_dir(dir.path().join("tests")).unwrap();
    fs::write(dir.path().join("tests/case.rs"), source).unwrap();
    Command::new(env!("CARGO_BIN_EXE_pg-policy"))
        .arg("--test-sources")
        .arg(dir.path())
        .output()
        .unwrap()
}

#[test]
fn rejects_raw_lf_split_and_host_home_with_locations() {
    let home = ["/home", "/fixture-user/data"].concat();
    let output = check(&format!(
        "fn test() {{\nlet text = std::fs::read_to_string(\"input\").unwrap();\nlet jobs = text.split_once(\"\\njobs:\\n\");\nlet home = \"{home}\";\n}}"
    ));
    assert!(!output.status.success());
    let diagnostics = String::from_utf8(output.stdout).unwrap();
    assert!(
        diagnostics.contains("tests/case.rs:3: CRLF"),
        "{diagnostics}"
    );
    assert!(
        diagnostics.contains("tests/case.rs:4: host home"),
        "{diagnostics}"
    );
}

#[test]
fn accepts_normalized_reads_and_unrelated_splits() {
    let output = check(
        r#"fn test() {
        let text = std::fs::read_to_string("input").unwrap().replace("\r\n", "\n");
        let jobs = text.split_once("\njobs:\n");
        let literal = "a\nb";
        let parts = literal.split('\n');
    }"#,
    );
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stdout)
    );
}

#[test]
fn rejects_alias_inline_and_discarded_normalization() {
    for source in [
        r#"fn test() { let s = std::fs::read_to_string("x").unwrap(); let alias = s; alias.split('\n'); }"#,
        r#"fn test() { std::fs::read_to_string("x").unwrap().split_once("\njob"); }"#,
        r#"fn test() { let s = std::fs::read_to_string("x").unwrap(); s.replace("\r\n", "\n"); s.split('\n'); }"#,
        r#"fn test() { let s = std::fs::read_to_string("x").unwrap(); let other = "".replace("\r\n", "\n"); s.split('\n'); }"#,
        r#"fn test() { let s = std::fs::read_to_string("x").unwrap(); s.split('\n').next().unwrap().replace("\r\n", "\n"); }"#,
        r#"fn test() { let s = std::fs::read_to_string("x").unwrap(); { let s = "safe"; } s.split('\n'); }"#,
    ] {
        let output = check(source);
        assert!(
            String::from_utf8_lossy(&output.stdout).contains("CRLF"),
            "{source}"
        );
    }
}

#[test]
fn accepts_reassigned_normalization_and_crlf_source() {
    let source = r#"fn test() {
        let mut s = std::fs::read_to_string("x").unwrap();
        s = s.replace("\r\n", "\n");
        s.split('\n');
    }"#;
    let output = check(&source.replace('\n', "\r\n"));
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stdout)
    );
}

#[test]
fn repository_test_sources_are_portable() {
    let output = Command::new(env!("CARGO_BIN_EXE_pg-policy"))
        .arg("--test-sources")
        .arg(std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../.."))
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stdout)
    );
}
