use super::*;
#[test]
fn reconcile_missing_pgid_is_unknown_and_never_settles_max_s() {
    use crate::reconcile_watch::{Step, StopPolicy, Watch};
    use pg_worker_core::{
        activity::Limits,
        hosts::Role,
        run::record::{RunRecord, Status},
    };
    struct Cleanup(std::path::PathBuf);
    impl Drop for Cleanup {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }
    let cleanup = Cleanup(std::env::temp_dir().join(pg_worker_core::run::record::new_run_id()));
    let root = &cleanup.0;
    let mut record = RunRecord::create(
        root,
        "run-test",
        "local",
        serde_json::json!({"proc":{"pid":123}})
            .as_object()
            .unwrap()
            .clone(),
        Status::Running,
        1.0,
    )
    .unwrap();
    let mut leader = Leader {
        identity: serde_json::from_value(record.get("proc").unwrap().clone()).unwrap(),
        fd: std::fs::File::open("/proc/self/stat").unwrap().into(),
    };
    let error = leader.empty().unwrap_err();
    assert_eq!(error.kind(), io::ErrorKind::Other);
    assert_eq!(error.to_string(), "Unknown");
    let mut child = Adopted::new(leader);
    let mut watch = Watch::new(
        Role::Implement,
        Limits::default(),
        0,
        (0, Some(1)),
        StopPolicy {
            grace_ms: 10_000,
            interval_ms: 2_000,
        },
        false,
    );
    watch.attach(&mut record, 1).unwrap();
    let before = std::fs::read(record.path()).unwrap();
    for now in [1000, 11_000, 13_000] {
        assert!(matches!(
            watch
                .advance(&mut child, root, &mut record, 1, now)
                .unwrap(),
            Step::Held
        ));
        assert_eq!(std::fs::read(record.path()).unwrap(), before);
        assert_eq!(record.status(), Some(Status::Running));
    }
}
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
