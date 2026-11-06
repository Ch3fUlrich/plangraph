#![cfg(unix)]
use std::process::Command;

#[test]
fn invalid_flags_exit_two_with_one_line() {
    for args in [
        vec!["--host-id", "Bad"],
        vec!["--host-id", ""],
        vec!["--max-runs", "0"],
        vec!["--max-runs", "65"],
        vec!["--max-runs", "abc"],
        vec!["--max-queued", "1025"],
        vec!["--placement", "shadow"],
        vec!["--placement", "on"],
        vec!["--placement", "bogus"],
    ] {
        let out = Command::new(env!("CARGO_BIN_EXE_pg-spawnd"))
            .arg("serve")
            .args(&args)
            .env_remove("PLANGRAPH_HOST")
            .env_remove("PLANGRAPH_PLACEMENT")
            .output()
            .unwrap();
        assert_eq!(out.status.code(), Some(2), "{args:?}");
        assert!(out.stdout.is_empty());
        assert_eq!(String::from_utf8(out.stderr).unwrap().lines().count(), 1);
    }
    for (key, value) in [("PLANGRAPH_HOST", "Bad"), ("PLANGRAPH_PLACEMENT", "on")] {
        let out = Command::new(env!("CARGO_BIN_EXE_pg-spawnd"))
            .arg("serve")
            .env_remove("PLANGRAPH_HOST")
            .env_remove("PLANGRAPH_PLACEMENT")
            .env(key, value)
            .output()
            .unwrap();
        assert_eq!(out.status.code(), Some(2));
        let stderr = String::from_utf8(out.stderr).unwrap();
        assert_eq!(stderr.lines().count(), 1);
        if key == "PLANGRAPH_PLACEMENT" {
            assert_eq!(stderr.trim(), "placement mode not implemented: off only");
        }
    }
}
