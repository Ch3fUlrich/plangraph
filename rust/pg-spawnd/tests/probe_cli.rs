#[path = "common/binary.rs"]
mod binary;
use std::process::{Command, Output};

fn cli(args: &[&str]) -> Output {
    output(Command::new(binary::spawnd()).args(args))
}

#[test]
fn prepare_spawnd_cli() {
    let executable = binary::spawnd();
    assert!(std::path::Path::new(executable).is_file());
    assert_eq!(binary::spawnd(), executable);
}

#[test]
fn unshared_cli_uses_cargos_executable_without_building() {
    if std::env::var_os("PG_TEST_UNSHARED_CLI").is_some() {
        assert_eq!(binary::spawnd(), env!("CARGO_BIN_EXE_pg-spawnd"));
        return;
    }
    let child = Command::new(std::env::current_exe().unwrap())
        .args([
            "--exact",
            "unshared_cli_uses_cargos_executable_without_building",
        ])
        .env("PG_TEST_UNSHARED_CLI", "1")
        .env_remove("CARGO_TARGET_DIR")
        .env_remove("CARGO_BUILD_TARGET_DIR")
        .output()
        .unwrap();
    assert!(
        child.status.success(),
        "{}",
        String::from_utf8_lossy(&child.stdout)
    );
    let checkout = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    for target in [
        None,
        Some(checkout.join("rust/target")),
        Some(checkout.join("rust/../target/custom")),
    ] {
        assert_eq!(
            binary::resolve(&checkout, target.as_deref(), |_| panic!(
                "unshared target built a CLI"
            )),
            env!("CARGO_BIN_EXE_pg-spawnd")
        );
    }
}

#[test]
fn cli_builds_stay_with_their_checkout() {
    let scratch = tempfile::tempdir().unwrap();
    let shared = scratch.path().join("shared");
    std::fs::create_dir(&shared).unwrap();
    let canonical_shared = shared.canonicalize().unwrap();
    let mut checkouts = Vec::new();
    for marker in ["first", "second"] {
        let root = scratch.path().join(marker);
        std::fs::create_dir_all(root.join("src")).unwrap();
        let manifest = root.join("Cargo.toml");
        std::fs::write(
            &manifest,
            "[package]\nname = \"fixture\"\nversion = \"0.1.0\"\nedition = \"2021\"\n[workspace]\n",
        )
        .unwrap();
        std::fs::write(
            root.join("src/main.rs"),
            format!("fn main() {{ println!({marker:?}); }}"),
        )
        .unwrap();
        let canonical_root = root.canonicalize().unwrap();
        let isolated = binary::resolve(&root, Some(&shared), |target| {
            assert!(target.starts_with(canonical_shared.join("cli-tests")));
            assert!(!target.starts_with(&canonical_root));
            binary::build(&manifest, target, "fixture")
        });
        binary::build(&manifest, &shared, "fixture");
        checkouts.push((marker, isolated));
    }
    // Both checkouts have built the same package name to the shared target;
    // the second has replaced its public executable. Each test keeps its own.
    assert_ne!(checkouts[0].1, checkouts[1].1);
    for (marker, executable) in checkouts {
        let result = Command::new(executable).output().unwrap();
        assert!(result.status.success());
        assert_eq!(String::from_utf8(result.stdout).unwrap().trim(), marker);
    }
}

#[test]
fn checkout_local_targets_use_canonical_paths() {
    let scratch = tempfile::tempdir().unwrap();
    let checkout = scratch.path().join("Checkout");
    let target = checkout.join("Target");
    std::fs::create_dir_all(&target).unwrap();
    let canonical_checkout = checkout.canonicalize().unwrap();
    let canonical_target = target.canonicalize().unwrap();
    for (root, target) in [
        (&checkout, canonical_target.clone()),
        (&canonical_checkout, target),
        (&checkout, canonical_target.join("missing/../custom")),
    ] {
        assert_eq!(
            binary::resolve(root, Some(&target), |_| panic!(
                "checkout-local target built a CLI"
            )),
            env!("CARGO_BIN_EXE_pg-spawnd")
        );
    }
}

#[test]
#[cfg(any(target_os = "linux", windows))]
#[ignore = "live capacity smoke test; requires host capacity access"]
fn probe_json() {
    let output = cli(&["probe", "--json", "--host-id", "code"]);
    assert!(output.status.success());
    assert!(output.stderr.is_empty());
    assert_eq!(output.stdout.iter().filter(|&&b| b == b'\n').count(), 1);
    let value: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    let object = value.as_object().unwrap();
    let keys = [
        "host_id",
        "os",
        "mem_total_mb",
        "mem_available_mb",
        "swap_total_mb",
        "swap_free_mb",
        "cpus",
        "load1",
        "load5",
        "load15",
        "ts_ms",
    ];
    assert_eq!(object.len(), keys.len());
    for key in keys {
        assert!(object.contains_key(key), "missing {key}");
    }
    assert_eq!(value["host_id"], "code");
    #[cfg(windows)]
    for key in ["load1", "load5", "load15"] {
        assert!(value[key].is_null());
    }
}

#[test]
fn invalid_arguments() {
    // a 33-char id, one over the cap; built at run time so it is no secret-looking literal
    let too_long = "a".repeat(33);
    for args in [
        vec!["probe", "--json", "--host-id", "Bad"],
        vec!["probe", "--json", "--host-id", ""],
        vec!["probe", "--json", "--host-id"],
        vec!["probe", "--host-id", "code"],
        vec!["probe", "--json", "--host-id", "--host-id", "x"],
        vec!["probe", "--json", "--host-id", "code", "--host-id", "x"],
        vec!["probe", "--json", "--json", "--host-id", "code"],
        vec![],
        vec!["probe", "--json", "--host-id", "a_b"],
        vec!["probe", "--json", "--host-id", too_long.as_str()],
        vec!["unknown"],
        vec!["probe", "--unknown"],
    ] {
        let output = cli(&args);
        assert_eq!(output.status.code(), Some(2));
        assert!(output.stdout.is_empty());
        assert_eq!(String::from_utf8(output.stderr).unwrap().lines().count(), 1);
    }
}

#[test]
fn help() {
    // --help/-h only as the first arg or a verb's first arg prints usage, exits 0.
    for args in [
        vec!["--help"],
        vec!["--help", "extra"],
        vec!["-h"],
        vec!["probe", "--help"],
        vec!["probe", "-h"],
        vec!["serve", "--help"],
        vec!["shim", "-h"],
        vec!["check-manage-key", "--help"],
    ] {
        let output = cli(&args);
        assert_eq!(output.status.code(), Some(0), "{args:?}");
        assert!(output.stderr.is_empty(), "{args:?}");
        assert!(String::from_utf8(output.stdout)
            .unwrap()
            .starts_with("Usage:"));
    }
    // Anywhere else --help/-h is an unknown flag (exit 2); as a --host-id value
    // it is that value and fails host-id validation.
    for args in [
        vec!["unknown", "-h"],
        vec!["probe", "--host-id", "--help"],
        vec!["probe", "--json", "--help"],
    ] {
        let output = cli(&args);
        assert_eq!(output.status.code(), Some(2), "{args:?}");
        assert!(output.stdout.is_empty(), "{args:?}");
        assert_eq!(String::from_utf8(output.stderr).unwrap().lines().count(), 1);
    }
}

#[test]
#[cfg(unix)]
fn non_utf8_argument_exits_two() {
    use std::os::unix::ffi::OsStrExt;
    for prefix in [vec![], vec!["probe", "--json", "--host-id"]] {
        let output = output(
            Command::new(binary::spawnd())
                .args(prefix)
                .arg(std::ffi::OsStr::from_bytes(&[0xff])),
        );
        assert_eq!(output.status.code(), Some(2));
        assert!(output.stdout.is_empty());
        assert_eq!(String::from_utf8(output.stderr).unwrap().lines().count(), 1);
    }
}

fn output(command: &mut Command) -> Output {
    use std::io::Read;
    let mut child = command
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .spawn()
        .unwrap();
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(2);
    let status = loop {
        if let Some(status) = child.try_wait().unwrap() {
            break status;
        }
        if std::time::Instant::now() >= deadline {
            let _ = child.kill();
            panic!("child deadline");
        }
        std::thread::sleep(std::time::Duration::from_millis(10));
    };
    let mut stdout = Vec::new();
    let mut stderr = Vec::new();
    child
        .stdout
        .take()
        .unwrap()
        .read_to_end(&mut stdout)
        .unwrap();
    child
        .stderr
        .take()
        .unwrap()
        .read_to_end(&mut stderr)
        .unwrap();
    Output {
        status,
        stdout,
        stderr,
    }
}
