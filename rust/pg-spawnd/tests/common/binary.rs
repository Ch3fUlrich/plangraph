//! CLI tests must not execute the mutable, shared Cargo output path.
use std::{path::Path, process::Command, sync::OnceLock};

pub(crate) fn spawnd() -> &'static str {
    static BINARY: OnceLock<String> = OnceLock::new();
    BINARY.get_or_init(|| {
        let manifest = Path::new(env!("CARGO_MANIFEST_DIR")).join("Cargo.toml");
        // CARGO_BIN_EXE_pg-spawnd names target/debug/pg-spawnd even when
        // different worktrees share that target. Another build can overwrite
        // it after Cargo releases its lock, including while tests are running.
        // Keep this suite's executable and Cargo fingerprints in this checkout.
        let target = test_target(&manifest);
        build(&manifest, &target, "pg-spawnd")
    })
}

pub(crate) fn test_target(manifest: &Path) -> std::path::PathBuf {
    manifest.parent().unwrap().join("target/cli-tests")
}

pub(crate) fn build(manifest: &Path, target: &Path, name: &str) -> String {
    let output = Command::new(env!("CARGO"))
        .args(["build", "--quiet", "--manifest-path"])
        .arg(manifest)
        .arg("--target-dir")
        .arg(target)
        .args(["--bin", name])
        .output()
        .expect("build checkout's CLI for integration tests");
    assert!(
        output.status.success(),
        "CLI build failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    target
        .join("debug")
        .join(format!("{name}{}", std::env::consts::EXE_SUFFIX))
        .to_str()
        .unwrap()
        .to_owned()
}
