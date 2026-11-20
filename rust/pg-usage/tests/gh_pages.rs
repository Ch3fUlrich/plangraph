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
fn merged_prs_come_from_concatenated_gh_pages() {
    let dir = tempfile::tempdir().unwrap();
    let projects = dir.path().join("projects");
    fs::create_dir_all(projects.join("p")).unwrap();
    let turn = json!({"type": "assistant", "timestamp": "2026-11-06T12:00:00Z",
        "message": {"id": "a", "usage": {"input_tokens": 1}}});
    fs::write(
        projects.join("p/s.jsonl"),
        format!(
            "{turn}
"
        ),
    )
    .unwrap();
    let out = Command::new(env!("CARGO_BIN_EXE_pg-usage"))
        .args([
            "--census",
            "--json",
            "--since",
            "2026-11-06",
            "--until",
            "2026-11-09",
        ])
        .arg("--projects")
        .arg(&projects)
        .env("PATH", gh_path(dir.path()))
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stdout)
    );
    assert_eq!(
        serde_json::from_slice::<Value>(&out.stdout).unwrap()["merged_prs"],
        2
    );
}
