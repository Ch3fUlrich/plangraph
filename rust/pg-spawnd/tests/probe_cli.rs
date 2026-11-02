use std::process::{Command, Output};

fn cli(args: &[&str]) -> Output {
    Command::new(env!("CARGO_BIN_EXE_pg-spawnd"))
        .args(args)
        .output()
        .unwrap()
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
        let output = Command::new(env!("CARGO_BIN_EXE_pg-spawnd"))
            .args(prefix)
            .arg(std::ffi::OsStr::from_bytes(&[0xff]))
            .output()
            .unwrap();
        assert_eq!(output.status.code(), Some(2));
        assert!(output.stdout.is_empty());
        assert_eq!(String::from_utf8(output.stderr).unwrap().lines().count(), 1);
    }
}
