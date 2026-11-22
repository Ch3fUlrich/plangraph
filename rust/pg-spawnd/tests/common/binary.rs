//! Resolve checkout-owned CLI binaries only when Cargo's target is shared.
use sha2::{Digest, Sha256};
use std::{
    path::{Component, Path, PathBuf},
    process::Command,
    sync::OnceLock,
};

#[test]
fn prepare_spawnd_cli() {
    // The deadline runner discovers this test even when a case filter hides it.
    println!(
        "\npg-test-fixture: {}",
        serde_json::to_string(spawnd()).unwrap()
    );
}

pub(crate) fn spawnd() -> &'static str {
    static BINARY: OnceLock<String> = OnceLock::new();
    BINARY.get_or_init(|| {
        let manifest = Path::new(env!("CARGO_MANIFEST_DIR")).join("Cargo.toml");
        let checkout = manifest.parent().unwrap().join("../..");
        let target = std::env::var_os("CARGO_TARGET_DIR")
            .or_else(|| std::env::var_os("CARGO_BUILD_TARGET_DIR"));
        // Cargo starts tests in the crate directory, which can differ from
        // the invocation directory that relative target variables refer to.
        // Cargo's integration-test scratch path records the actual target root.
        let target = target.map(PathBuf::from).map(|target| {
            if target.is_relative() {
                Path::new(env!("CARGO_TARGET_TMPDIR"))
                    .parent()
                    .unwrap()
                    .to_owned()
            } else {
                target
            }
        });
        resolve(&checkout, target.as_deref(), |target| {
            let path = binary_path(target, "pg-spawnd");
            if let Some(prepared) = std::env::var_os("PG_TEST_PREBUILT_SPAWND") {
                assert!(
                    prepared == path.as_os_str() && path.is_file(),
                    "expected the prepared checkout CLI at {}",
                    path.display()
                );
                return path.to_str().unwrap().to_owned();
            }
            build(&manifest, target, "pg-spawnd")
        })
    })
}

pub(crate) fn resolve(
    checkout: &Path,
    target: Option<&Path>,
    build_cli: impl FnOnce(&Path) -> String,
) -> String {
    let Some(target) = target else {
        return env!("CARGO_BIN_EXE_pg-spawnd").to_owned();
    };
    let checkout = absolute(checkout);
    let target = absolute(target);
    if target.starts_with(&checkout) {
        return env!("CARGO_BIN_EXE_pg-spawnd").to_owned();
    }
    // Cargo's uplifted target/debug executable can be replaced by another
    // checkout after Cargo releases its lock. Isolate fingerprints as well as
    // the executable, while keeping all extra build output in the shared cap.
    let hash = Sha256::digest(checkout.as_os_str().as_encoded_bytes());
    let key = format!("{hash:x}");
    build_cli(&target.join("cli-tests").join(&key[..16]))
}

fn absolute(path: &Path) -> PathBuf {
    // Compare filesystem spellings on both sides: Windows canonicalization
    // resolves verbatim prefixes, case and short names for existing paths.
    if let Ok(canonical) = path.canonicalize() {
        return canonical;
    }
    // A target need not exist yet. Canonicalize its existing ancestors, then
    // retain the missing suffix so it uses the same spelling as the checkout.
    let mut result = PathBuf::new();
    for component in std::env::current_dir().unwrap().join(path).components() {
        match component {
            Component::CurDir => {}
            Component::ParentDir => {
                result.pop();
            }
            other => {
                result.push(other.as_os_str());
                if let Ok(canonical) = result.canonicalize() {
                    result = canonical;
                }
            }
        }
    }
    result
}

#[doc = "deadline-build: fixture=prepare_spawnd_cli"]
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
    binary_path(target, name).to_str().unwrap().to_owned()
}

fn binary_path(target: &Path, name: &str) -> PathBuf {
    target
        .join("debug")
        .join(format!("{name}{}", std::env::consts::EXE_SUFFIX))
}
