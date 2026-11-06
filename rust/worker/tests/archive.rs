//! Shared recording policy: a build's evidence can only be created once.
use std::io::Write;
use std::path::{Path, PathBuf};

pub fn validate_pins(root: &Path) {
    for release in std::fs::read_dir(root.join("releases")).unwrap() {
        let dir = release.unwrap().path();
        let manifest: serde_json::Value = serde_json::from_slice(
            &std::fs::read(dir.join("manifest.json")).unwrap()).unwrap();
        assert!(!manifest["source_commit"].as_str().unwrap().is_empty());
        for (name, digest) in manifest["sha256"].as_object().unwrap() {
            assert!([root.join(name), dir.join(name)].iter().any(|path| {
                if !path.is_file() { return false; }
                let output = std::process::Command::new("sha256sum")
                    .arg(path).output().expect("sha256sum available");
                output.status.success() && String::from_utf8(output.stdout).unwrap()
                    .split_whitespace().next() == digest.as_str()
            }), "released fixture {name} changed without preserving its old bytes");
        }
    }
}

#[test]
fn released_pins_are_preserved() {
    validate_pins(Path::new(concat!(env!("CARGO_MANIFEST_DIR"), "/tests/fixtures")));
}

pub fn directory(root: &Path) -> PathBuf {
    let build = std::env::var("PG_BUILD_ID").expect("recording requires PG_BUILD_ID");
    assert!(pg_worker::buildid::validate(&build), "invalid recording build id");
    let dir = root.join(build);
    std::fs::create_dir_all(&dir).expect("create build archive");
    dir
}

pub fn append(path: &Path, bytes: &[u8]) {
    let mut file = std::fs::OpenOptions::new().write(true).create_new(true)
        .open(path).expect("archive entries are immutable; choose a new build id");
    file.write_all(bytes).expect("write archive entry");
}

#[test]
fn recording_cannot_overwrite_released_evidence() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("happy.json");
    append(&path, b"original");
    assert!(std::panic::catch_unwind(|| append(&path, b"replacement")).is_err());
    assert_eq!(std::fs::read(path).unwrap(), b"original");
}
