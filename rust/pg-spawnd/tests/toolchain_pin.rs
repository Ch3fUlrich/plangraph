//! Root-level Cargo invocations and builds under rust/ must share one compiler.
use std::{path::Path, process::Command};

#[test]
fn root_and_rust_use_the_pinned_release() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let pin = std::fs::read_to_string(root.join("rust-toolchain.toml")).unwrap();
    let channel = pin
        .lines()
        .find_map(|line| {
            let (key, value) = line.split_once('=')?;
            (key.trim() == "channel").then(|| value.trim().trim_matches('"'))
        })
        .expect("toolchain channel");
    for directory in [root.clone(), root.join("rust")] {
        // Cargo sets this variable for test processes; let rustup resolve by cwd.
        let output = Command::new("rustc")
            .arg("--version")
            .env_remove("RUSTUP_TOOLCHAIN")
            .current_dir(&directory)
            .output()
            .expect("run rustc --version");
        assert!(output.status.success(), "{directory:?}: {output:?}");
        let version = String::from_utf8(output.stdout).unwrap();
        assert_eq!(
            version.split_whitespace().take(2).collect::<Vec<_>>(),
            ["rustc", channel],
            "compiler from {directory:?}: {version}"
        );
    }
}
