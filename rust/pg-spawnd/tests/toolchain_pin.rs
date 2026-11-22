//! Root-level Cargo invocations and builds under rust/ must share one compiler.
#[cfg(test)]
#[path = "../../test_support/python.rs"]
mod test_python;

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
    let sysroot = Command::new("rustc")
        .args(["--print", "sysroot"])
        .output()
        .unwrap();
    assert!(sysroot.status.success(), "{sysroot:?}");
    let direct = Path::new(String::from_utf8(sysroot.stdout).unwrap().trim())
        .join("bin")
        .join(format!("rustc{}", std::env::consts::EXE_SUFFIX));
    for directory in [root.clone(), root.join("rust")] {
        for compiler in [Path::new("rustc"), direct.as_path()] {
            // Rustup resolves by cwd; direct sysroots (Dell) must already be pinned.
            let output = Command::new(compiler)
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
}

#[test]
fn ci_temporal_inputs_select_the_worker_job() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    for path in [
        "rust/pg-spawnd/src/bin/ci-temporal.rs",
        "rust/pg-spawnd/Cargo.toml",
        "rust/Cargo.toml",
        "rust/Cargo.lock",
    ] {
        let output = test_python::command()
            .args(["tools/ci_select.py", "select", "--files", path])
            .current_dir(&root)
            .output()
            .unwrap();
        assert!(output.status.success(), "{output:?}");
        let selected = String::from_utf8(output.stdout).unwrap();
        assert!(
            selected.lines().any(|line| line == "rust-worker=true"),
            "{path}: {selected}"
        );
    }
}

#[test]
fn worker_and_temporal_helper_share_the_cached_target() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let workflow = std::fs::read_to_string(root.join(".github/workflows/tests.yml"))
        .unwrap()
        .replace("\r\n", "\n");
    let job = workflow
        .split("\n  rust-worker:\n")
        .nth(1)
        .unwrap()
        .split("\n  rust-spike:\n")
        .next()
        .unwrap();
    let (configuration, steps) = job.split_once("    steps:\n").unwrap();
    assert!(
        !configuration.contains("CARGO_TARGET_DIR"),
        "a job-level target dir would override the self-hosted one"
    );
    let step = |name: &str| -> &str {
        steps
            .split(&format!("      - name: {name}\n"))
            .nth(1)
            .unwrap_or_else(|| panic!("missing step {name}"))
            .split("\n      - ")
            .next()
            .unwrap()
    };
    let hosted = step("Hosted cargo build settings");
    assert!(hosted.contains("env.CI_SELF_HOSTED_RUN != '1'"));
    assert!(hosted.contains(
        "echo \"CARGO_TARGET_DIR=$GITHUB_WORKSPACE/rust/worker/target\" >> \"$GITHUB_ENV\""
    ));
    let self_hosted = step("Self-hosted cargo build settings");
    assert!(self_hosted.contains("env.CI_SELF_HOSTED_RUN == '1'"));
    assert!(self_hosted.contains("echo \"CARGO_TARGET_DIR=$target\" >> \"$GITHUB_ENV\""));
    // Every later step (worker tests and the temporal helper) reads the one exported dir.
    assert_eq!(steps.matches("CARGO_TARGET_DIR=").count(), 2);
    let cache = steps
        .split("uses: actions/cache@v4\n")
        .nth(1)
        .unwrap()
        .split("      - ")
        .next()
        .unwrap();
    assert!(cache
        .lines()
        .any(|line| line.trim() == "rust/worker/target"));
    let key = cache
        .lines()
        .find(|line| line.trim_start().starts_with("key:"))
        .unwrap();
    for input in ["rust/Cargo.lock", "rust/pg-spawnd/Cargo.toml"] {
        assert!(key.contains(input), "cache key misses {input}");
    }
}

#[cfg(unix)]
#[test]
fn dell_provisions_the_pin_before_resolving_the_direct_sysroot() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let output = test_python::command()
        .args(["-c", "import sys; sys.path.insert(0, 'tools'); from dell_ci_jobs import isolated_script; print(isolated_script('true', '/source', '/job', cargo_deps=True))"])
        .current_dir(&root)
        .output().unwrap();
    assert!(output.status.success(), "{output:?}");
    let script = String::from_utf8(output.stdout).unwrap();
    let start = script
        .find("toolchain_channel=")
        .expect("explicit pinned toolchain");
    let end = script[start..].find("protoc=").unwrap() + start;
    let fixture = tempfile::tempdir().unwrap();
    std::fs::create_dir_all(fixture.path().join("checkout")).unwrap();
    std::fs::copy(
        root.join("rust-toolchain.toml"),
        fixture.path().join("checkout/rust-toolchain.toml"),
    )
    .unwrap();
    let pin = std::fs::read_to_string(root.join("rust-toolchain.toml")).unwrap();
    let channel = pin
        .lines()
        .find(|line| line.starts_with("channel ="))
        .unwrap()
        .split('"')
        .nth(1)
        .unwrap();
    let prelude = r#"set -eu
rustup() {
    printf '%s\n' "$*" >> "$scratch/calls"
    case "$1" in
        toolchain) test "$2" = install && test "$3" = "$expected_pin" ;;
        run) test "$2" = "$expected_pin"; printf '%s\n' /pinned-sysroot ;;
        *) exit 91 ;;
    esac
}
rustc() { exit 92; }
"#;
    let output = Command::new("sh")
        .arg("-c")
        .arg(format!(
            "{prelude}{}\nprintf '%s\\n' \"$toolchain\" \"$cargo_bin\"",
            &script[start..end]
        ))
        .env("scratch", fixture.path())
        .env("expected_pin", channel)
        .output()
        .unwrap();
    assert!(output.status.success(), "{output:?}");
    assert_eq!(
        String::from_utf8(output.stdout).unwrap(),
        "/pinned-sysroot\n/pinned-sysroot/bin\n"
    );
    let calls = std::fs::read_to_string(fixture.path().join("calls")).unwrap();
    assert!(calls.starts_with(&format!("toolchain install {channel} ")));
    assert!(calls.contains("--component rustfmt,clippy"));
    assert!(calls.contains("--target x86_64-pc-windows-gnu"));
    assert!(calls.ends_with(&format!("run {channel} rustc --print sysroot\n")));
    assert!(script.contains("PATH=\"$cargo_bin:/usr/bin:/bin\""));
}
