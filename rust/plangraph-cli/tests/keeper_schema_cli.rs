//! The public `plangraph land-train keeper-schema` works from a clean source checkout: no Python path,
//! home or virtual environment is set, and the reader is the one the binary carries.
use std::path::Path;
use std::process::Command;

fn git(dir: &Path, args: &[&str]) -> String {
    let out = Command::new("git")
        .args(["-c", "user.name=t", "-c", "user.email=t@example.invalid"])
        .args(args)
        .current_dir(dir)
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    String::from_utf8_lossy(&out.stdout).trim().to_owned()
}

fn commit(dir: &Path, entries: u32) -> String {
    let body: Vec<_> = (1..=entries)
        .map(|v| format!("Migration({v}, a, X)"))
        .collect();
    let file = dir.join("src/plangraph/keeper/schema_pg.py");
    std::fs::create_dir_all(file.parent().unwrap()).unwrap();
    std::fs::write(file, format!("MIGRATIONS = ({})\n", body.join(", "))).unwrap();
    git(dir, &["add", "-A"]);
    git(dir, &["commit", "-q", "-m", &format!("v{entries}")]);
    git(dir, &["rev-parse", "HEAD"])
}

#[test]
fn keeper_schema_reads_both_versions_with_the_python_environment_cleared() {
    let dir = tempfile::tempdir().unwrap();
    git(dir.path(), &["init", "-q", "-b", "main"]);
    let (base, head) = (commit(dir.path(), 2), commit(dir.path(), 3));
    let home = tempfile::tempdir().unwrap();
    let out = Command::new(env!("CARGO_BIN_EXE_plangraph"))
        .args(["land-train", "keeper-schema", "--repo"])
        .arg(dir.path())
        .args(["--base", &base, "--head", &head])
        .env_remove("PYTHONPATH")
        .env_remove("PYTHONHOME")
        .env_remove("VIRTUAL_ENV")
        .env_remove("PG_SPAWND_PYTHON")
        .env("HOME", home.path())
        .current_dir(home.path())
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    assert_eq!(
        String::from_utf8_lossy(&out.stdout).trim(),
        r#"{"keeper_schema":{"base":2,"head":3},"migration":null}"#
    );
}
