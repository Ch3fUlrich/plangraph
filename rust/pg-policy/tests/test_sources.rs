use std::{fs, process::Command};

fn check(source: &str) -> std::process::Output {
    check_with_runner(source, None)
}

fn check_with_runner(source: &str, runner: Option<&str>) -> std::process::Output {
    let dir = tempfile::tempdir().unwrap();
    fs::create_dir(dir.path().join("tests")).unwrap();
    fs::write(dir.path().join("tests/case.rs"), source).unwrap();
    if let Some(runner) = runner {
        let dir = dir.path().join("rust/worker-core/src/bin");
        fs::create_dir_all(&dir).unwrap();
        fs::write(dir.join("pg-test-deadline.rs"), runner).unwrap();
    }
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

#[test]
fn a_second_nested_cargo_case_needs_its_own_build_override() {
    let output = check_with_runner(
        r#"
#[test]
#[doc = "deadline-build: case=first"]
fn first() { std::process::Command::new("cargo").arg("build").status().unwrap(); }
#[test]
fn second() { let cargo = env!("CARGO"); std::process::Command::new(cargo).status().unwrap(); }
#[test]
fn prebuilt() { std::process::Command::new(env!("CARGO_BIN_EXE_fixture")).status().unwrap(); }
"#,
        Some(
            r#"fn build_deadline(suite: &str, case: &str) -> Option<u64> {
            match (suite, case) { ("case", "first") => Some(600), _ => None }
        }"#,
        ),
    );
    let diagnostics = String::from_utf8(output.stdout).unwrap();
    assert!(!output.status.success(), "{diagnostics}");
    assert!(
        diagnostics.contains("tests/case.rs:6: second"),
        "{diagnostics}"
    );
    assert_eq!(diagnostics.lines().count(), 1, "{diagnostics}");
}

#[test]
fn an_override_for_another_case_does_not_cover_a_cargo_build() {
    let output = check(
        r#"
#[test]
fn first() {}
#[test]
#[doc = "deadline-build: case=first"]
fn second() { std::process::Command::new("cargo").status().unwrap(); }
"#,
    );
    let diagnostics = String::from_utf8(output.stdout).unwrap();
    assert!(!output.status.success(), "{diagnostics}");
    assert!(
        diagnostics.contains("tests/case.rs:6: second"),
        "{diagnostics}"
    );
}

#[test]
fn a_declared_runner_must_have_the_named_override() {
    let output = check_with_runner(
        r#"#[test]
#[doc = "deadline-build: case=build"]
fn build() { std::process::Command::new(env!("CARGO", "Cargo needed")); }"#,
        Some(
            r#"fn build_deadline(suite: &str, case: &str) -> Option<u64> {
            match (suite, case) { ("another_suite", "build") => Some(600), _ => None }
        }"#,
        ),
    );
    let diagnostics = String::from_utf8(output.stdout).unwrap();
    assert!(!output.status.success(), "{diagnostics}");
    assert!(
        diagnostics.contains("tests/case.rs:3: build"),
        "{diagnostics}"
    );
}
