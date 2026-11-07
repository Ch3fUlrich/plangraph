//! Shared recording policy: a build's evidence can only be created once.
use sha2::{Digest, Sha256};
use std::io::Write;
use std::path::{Path, PathBuf};

pub fn validate_pins(root: &Path) {
    for release in std::fs::read_dir(root.join("releases")).unwrap() {
        let dir = release.unwrap().path();
        let manifest: serde_json::Value =
            serde_json::from_slice(&std::fs::read(dir.join("manifest.json")).unwrap()).unwrap();
        assert!(!manifest["source_commit"].as_str().unwrap().is_empty());
        for (name, digest) in manifest["sha256"].as_object().unwrap() {
            assert!(
                [root.join(name), dir.join(name)].iter().any(|path| {
                    if !path.is_file() {
                        return false;
                    }
                    format!("{:x}", Sha256::digest(std::fs::read(path).unwrap()))
                        == digest.as_str().unwrap()
                }),
                "released fixture {name} changed without preserving its old bytes"
            );
        }
    }
}

#[test]
fn released_pins_are_preserved() {
    validate_pins(Path::new(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/tests/fixtures"
    )));
}

pub fn directory(root: &Path) -> PathBuf {
    static BUILD: std::sync::OnceLock<String> = std::sync::OnceLock::new();
    let build = std::env::var("PG_BUILD_ID").unwrap_or_else(|_| {
        BUILD
            .get_or_init(|| {
                format!(
                    "e2e-{}-{}",
                    std::process::id(),
                    std::time::SystemTime::now()
                        .duration_since(std::time::UNIX_EPOCH)
                        .unwrap()
                        .as_nanos()
                )
            })
            .clone()
    });
    assert!(
        pg_worker::buildid::validate(&build),
        "invalid recording build id"
    );
    let dir = root.join(build);
    std::fs::create_dir_all(&dir).expect("create build archive");
    dir
}

pub fn append(path: &Path, bytes: &[u8]) {
    let file = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(path);
    let mut file = match file {
        Ok(file) => file,
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => return,
        Err(error) => panic!("create archive entry: {error}"),
    };
    file.write_all(bytes).expect("write archive entry");
}

#[test]
fn recording_cannot_overwrite_released_evidence() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("happy.json");
    append(&path, b"original");
    append(&path, b"replacement");
    assert_eq!(std::fs::read(path).unwrap(), b"original");
}
