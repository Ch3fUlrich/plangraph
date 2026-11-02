use std::process::{Command, Output};

fn cli(args: &[&str]) -> Output {
    Command::new(env!("CARGO_BIN_EXE_pg-spawnd"))
        .args(args)
        .output()
        .unwrap()
}

#[test]
#[cfg(any(target_os = "linux", windows))]
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
    for args in [
        vec!["probe", "--json", "--host-id", "Bad"],
        vec!["probe", "--json", "--host-id", ""],
        vec!["probe", "--json", "--host-id"],
        vec!["probe", "--json", "--host-id", "a_b"],
        vec![
            "probe",
            "--json",
            "--host-id",
            "abcdefghijklmnopqrstuvwxyz1234567",
        ],
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
    let output = cli(&["--help"]);
    assert!(output.status.success());
    assert!(output.stderr.is_empty());
    assert!(String::from_utf8(output.stdout)
        .unwrap()
        .starts_with("Usage:"));
}
