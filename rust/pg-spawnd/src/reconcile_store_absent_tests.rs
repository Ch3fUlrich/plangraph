use super::*;

#[test]
fn verified_absent_inventory_requires_gone() {
    let dir = StateDir::new();
    let (_root, scope) = cgroup_tests::ScopeRoot::new(&dir.0);
    let pid = std::process::id();
    let stat = std::fs::read_to_string(format!("/proc/{pid}/stat")).unwrap();
    let start: u64 = stat
        .rsplit_once(')')
        .unwrap()
        .1
        .split_whitespace()
        .nth(19)
        .unwrap()
        .parse()
        .unwrap();
    let mut record = dir.record();
    record.set(
        "proc",
        json!({"pid":pid,"start_time":start,
        "group":scope.to_str().unwrap(),"pgid":null}),
    );
    record.save().unwrap();
    let row = inventory(&dir.0).unwrap().remove(0);
    std::fs::remove_file(record.path()).unwrap();
    std::fs::write(scope.join("cgroup.events"), "populated 1\n").unwrap();
    std::fs::write(scope.join("cgroup.procs"), pid.to_string()).unwrap();
    assert!(!orphan_gone(&dir.0, &row).unwrap());
    std::fs::write(scope.join("cgroup.events"), "populated 0\n").unwrap();
    assert!(orphan_gone(&dir.0, &row).unwrap());
}
