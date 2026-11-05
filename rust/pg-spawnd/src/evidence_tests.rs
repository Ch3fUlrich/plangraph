use super::*;
use crate::evidence_row::{ObservationKind, ObservationRow};
struct Temp(PathBuf);
impl Temp {
    fn new() -> Self {
        let p = std::env::temp_dir().join(pg_worker_core::run::record::new_run_id());
        fs::create_dir(&p).unwrap();
        Self(p)
    }
}
impl Drop for Temp {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}
fn observation(ts: i64) -> EvidenceRow {
    EvidenceRow::Observation(ObservationRow {
        leg_key: "leg".into(),
        ts,
        kind: ObservationKind::Canary,
        until: None,
        state: None,
        first_seen: None,
        phase: None,
    })
}
#[test]
fn child_append() {
    if let Some(path) = std::env::var_os("PG_EVIDENCE_TEST_STATE") {
        let store = EvidenceStore::open(Path::new(&path), true).unwrap();
        for ts in 0..30 {
            store.append(&observation(ts)).unwrap();
        }
    }
}
#[test]
fn two_process_append_no_loss() {
    let temp = Temp::new();
    let mut children = Vec::new();
    for _ in 0..2 {
        children.push(
            std::process::Command::new(std::env::current_exe().unwrap())
                .args(["--exact", "evidence::tests::child_append"])
                .env("PG_EVIDENCE_TEST_STATE", &temp.0)
                .spawn()
                .unwrap(),
        );
    }
    for mut child in children {
        assert!(child.wait().unwrap().success());
    }
    assert_eq!(
        EvidenceStore::open(&temp.0, true)
            .unwrap()
            .window(0, 100)
            .unwrap()
            .observations
            .len(),
        30
    );
}
#[test]
fn torn_final_line_read_then_append() {
    let temp = Temp::new();
    let store = EvidenceStore::open(&temp.0, true).unwrap();
    store.append(&observation(1)).unwrap();
    let path = temp.0.join("evidence/rows.jsonl");
    fs::OpenOptions::new()
        .append(true)
        .open(&path)
        .unwrap()
        .write_all(b"{torn")
        .unwrap();
    assert_eq!(store.window(0, 10).unwrap().observations.len(), 1);
    store.append(&observation(2)).unwrap();
    assert_eq!(store.window(0, 10).unwrap().observations.len(), 2);
    fs::write(path, b"invalid\n").unwrap();
    assert!(store.append(&observation(3)).is_err());
}
#[test]
fn open_first_without_cleared_restart() {
    let temp = Temp::new();
    let store = EvidenceStore::open(&temp.0, true).unwrap();
    let mut o = ObservationRow {
        leg_key: "leg".into(),
        ts: 1,
        kind: ObservationKind::OmniRouteSeen,
        until: None,
        state: Some("parked".into()),
        first_seen: Some(1),
        phase: Some(Phase::First),
    };
    store.append(&EvidenceRow::Observation(o.clone())).unwrap();
    assert_eq!(
        EvidenceStore::open(&temp.0, true)
            .unwrap()
            .open_episodes()
            .unwrap()
            .len(),
        1
    );
    o.phase = Some(Phase::Cleared);
    o.ts = 100;
    store.append(&EvidenceRow::Observation(o)).unwrap();
    assert!(store.open_episodes().unwrap().is_empty());
}
#[test]
fn switch_off_no_rows_records_remain() {
    let state = std::env::temp_dir().join(pg_worker_core::run::record::new_run_id());
    let store = EvidenceStore::open(&state, false).unwrap();
    assert!(store.window(0, 10).unwrap().outcomes.is_empty());
    assert!(!state.exists());
}
