#![cfg(target_os = "linux")]
use pg_reap::orphans::{inspect, Evidence};
use std::fs;
#[test]
fn requires_orphan_start_and_exact_cwd() {
    let root = tempfile::tempdir().unwrap();
    let cwd = tempfile::tempdir().unwrap();
    let proc = root.path().join("123");
    fs::create_dir(&proc).unwrap();
    let mut fields = vec!["0"; 22];
    fields[0] = "S";
    fields[1] = "1";
    fields[19] = "42";
    fs::write(
        proc.join("stat"),
        format!("123 (test) {}", fields.join(" ")),
    )
    .unwrap();
    std::os::unix::fs::symlink(cwd.path(), proc.join("cwd")).unwrap();
    let mut evidence = Evidence {
        pid: 123,
        start_ticks: 42,
        cwd: cwd.path().into(),
    };
    assert!(inspect(root.path(), &evidence).unwrap());
    evidence.start_ticks = 43;
    assert!(!inspect(root.path(), &evidence).unwrap());
    evidence.start_ticks = 42;
    evidence.cwd = root.path().into();
    assert!(!inspect(root.path(), &evidence).unwrap());
    fields[1] = "2";
    fs::write(
        proc.join("stat"),
        format!("123 (test) {}", fields.join(" ")),
    )
    .unwrap();
    evidence.cwd = cwd.path().into();
    assert!(!inspect(root.path(), &evidence).unwrap());
}
