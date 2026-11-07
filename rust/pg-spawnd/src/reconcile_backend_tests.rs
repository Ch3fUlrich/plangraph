use super::*;
#[test]
fn reconcile_leader_proves_empty_after_reaping() {
    use std::os::unix::process::CommandExt;
    let mut child = std::process::Command::new("sleep")
        .arg("60")
        .process_group(0)
        .spawn()
        .unwrap();
    let pid = child.id();
    let stat = std::fs::read_to_string(format!("/proc/{pid}/stat")).unwrap();
    let start_time = stat
        .rsplit_once(')')
        .unwrap()
        .1
        .split_whitespace()
        .nth(19)
        .unwrap()
        .parse()
        .unwrap();
    let mut leader = Leader::open(Proc {
        pid,
        start_time: Some(start_time),
        group: None,
        pgid: Some(pid),
    })
    .unwrap();
    assert!(!leader.empty().unwrap());
    leader.signal(true).unwrap();
    child.wait().unwrap();
    assert!(leader.empty().unwrap());
}
