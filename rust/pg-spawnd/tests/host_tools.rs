use std::process::Command;

#[test]
fn wsl_cache_guard_exposes_the_host_command_and_refuses_zero() {
    let help = Command::new(env!("CARGO_BIN_EXE_pg-spawnd"))
        .args(["host", "wsl-cache-guard", "--help"])
        .output()
        .unwrap();
    assert!(help.status.success(), "{help:?}");
    assert!(String::from_utf8_lossy(&help.stdout).contains("--min-free-mb"));
    let invalid = Command::new(env!("CARGO_BIN_EXE_pg-spawnd"))
        .args(["host", "wsl-cache-guard", "--min-free-mb", "0"])
        .output()
        .unwrap();
    assert!(!invalid.status.success(), "{invalid:?}");
    assert!(String::from_utf8_lossy(&invalid.stderr).contains("positive integer"));
}
