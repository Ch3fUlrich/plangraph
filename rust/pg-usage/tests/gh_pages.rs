//! A fake `gh` first on PATH prints two concatenated GraphQL pages and refuses `--slurp`.
use serde_json::{json, Value};
use std::{ffi::OsString, fs, path::Path, process::Command};

const FAKE_GH: &str = r#"fn main() {
    let a: Vec<String> = std::env::args().collect();
    if a.iter().any(|x| x == "--slurp") { std::process::exit(2) }
    if a[1] == "repo" { println!("o/r") } else { print!("{}", PAGES) }
}"#;

fn gh_path(dir: &Path) -> OsString {
    let bin = dir.join("bin");
    fs::create_dir_all(&bin).unwrap();
    let page = |n: u32| {
        json!({"data": {"search": {"issueCount": 2,
        "nodes": [{"number": n, "mergedAt": "2026-11-06T12:00:00Z"}]}}})
    };
    let pages = page(1).to_string() + &page(2).to_string();
    let source = dir.join("gh.rs");
    fs::write(
        &source,
        format!(
            "const PAGES: &str = {pages:?};
{FAKE_GH}"
        ),
    )
    .unwrap();
    let exe = bin.join(format!("gh{}", std::env::consts::EXE_SUFFIX));
    assert!(Command::new("rustc")
        .arg(&source)
        .arg("-o")
        .arg(&exe)
        .status()
        .unwrap()
        .success());
    let old = std::env::var_os("PATH").unwrap();
    std::env::join_paths(std::iter::once(bin).chain(std::env::split_paths(&old))).unwrap()
}

#[test]
fn fake_gh_refuses_slurp() {
    let dir = tempfile::tempdir().unwrap();
    let run = |flag| {
        Command::new("gh")
            .args(["api", flag])
            .env("PATH", gh_path(dir.path()))
            .status()
    };
    assert!(!run("--slurp").unwrap().success());
}
