use super::*;
use crate::evidence_row::{ObservationKind, ObservationRow};
pub(super) struct Temp(pub(super) PathBuf);
impl Temp {
    pub(super) fn new() -> Self {
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
pub(super) fn observation(ts: i64) -> EvidenceRow {
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
        for kind in [
            ObservationKind::OmniRouteParked,
            ObservationKind::OmniRouteSeen,
            ObservationKind::OmniRouteStale,
        ] {
            for phase in [Phase::First, Phase::Cleared] {
                store
                    .append(&EvidenceRow::Observation(ObservationRow {
                        leg_key: "leg".into(),
                        ts: 1,
                        kind: kind.clone(),
                        until: if kind == ObservationKind::OmniRouteParked {
                            Some(120)
                        } else {
                            None
                        },
                        state: Some("parked".into()),
                        first_seen: Some(1),
                        phase: (kind == ObservationKind::OmniRouteSeen).then_some(phase),
                    }))
                    .unwrap();
            }
        }
    }
}
#[test]
fn all_observation_keys_two_process_append_once() {
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
        34
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
pub(super) fn outcome(ts: i64) -> EvidenceRow {
    serde_json::from_value(serde_json::json!({"type":"outcome","row":{
        "leg_key":"leg","route_key":"route","ts":ts,"role":"review",
        "outcome":"OK","run_id":"run-test","attempt":0,
        "duration_s":600,"reset_at":null,"reset_assumed":false,
        "reset_clamped":false,"open_tools_at_kill":null,"s_eff_s":600,"line":""
    }}))
    .unwrap()
}
#[test]
fn child_outcome_append() {
    let Some(path) = std::env::var_os("PG_OUTCOME_TEST_STATE") else {
        return;
    };
    let state = Path::new(&path);
    let id = std::env::var("PG_OUTCOME_CHILD").unwrap();
    let store = EvidenceStore::open(state, true).unwrap();
    fs::write(state.join(format!("ready-{id}")), b"").unwrap();
    let start = Instant::now();
    while !state.join("start").exists() {
        assert!(start.elapsed() < Duration::from_secs(10));
        std::thread::yield_now();
    }
    for ts in 0..30 {
        let EvidenceRow::Outcome(mut row) = outcome(ts) else {
            unreachable!()
        };
        row.run_id = format!("run-{id}x{ts}");
        assert!(store.append(&EvidenceRow::Outcome(row.clone())).unwrap());
        let mut shared = row.clone();
        shared.run_id = format!("run-shared{ts}");
        let record = json!({"attempts":[{"outcome":shared}]});
        store.backfill(std::slice::from_ref(&record)).unwrap();
        assert_eq!(store.backfill(&[record]).unwrap(), 0);
    }
}
#[test]
fn two_process_append_no_loss() {
    let temp = Temp::new();
    let mut children = Vec::new();
    for id in 0..2 {
        children.push(
            std::process::Command::new(std::env::current_exe().unwrap())
                .args(["--exact", "evidence::tests::child_outcome_append"])
                .env("PG_OUTCOME_TEST_STATE", &temp.0)
                .env("PG_OUTCOME_CHILD", id.to_string())
                .spawn()
                .unwrap(),
        );
    }
    let start = Instant::now();
    while !(0..2).all(|id| temp.0.join(format!("ready-{id}")).exists()) {
        assert!(start.elapsed() < Duration::from_secs(10));
        std::thread::yield_now();
    }
    fs::write(temp.0.join("start"), b"").unwrap();
    for mut child in children {
        assert!(child.wait().unwrap().success());
    }
    let store = EvidenceStore::open(&temp.0, true).unwrap();
    let rows = store.window(0, 30).unwrap().outcomes;
    assert_eq!(rows.len(), 90);
    assert_eq!(store.read().unwrap().0.len(), 90);
}
#[test]
fn parked_no_time_phase_does_not_duplicate() {
    let temp = Temp::new();
    let store = EvidenceStore::open(&temp.0, true).unwrap();
    let mut row = ObservationRow {
        leg_key: "leg".into(),
        ts: 1,
        kind: ObservationKind::OmniRouteParked,
        until: None,
        state: Some("parked".into()),
        first_seen: Some(1),
        phase: None,
    };
    assert!(store
        .append(&EvidenceRow::Observation(row.clone()))
        .unwrap());
    row.ts = 2;
    assert!(!store.append(&EvidenceRow::Observation(row)).unwrap());
    assert_eq!(store.window(0, 10).unwrap().observations.len(), 1);
}
#[test]
fn last_outcome_wins() {
    let temp = Temp::new();
    let store = EvidenceStore::open(&temp.0, true).unwrap();
    store.append(&outcome(1)).unwrap();
    store.append(&outcome(20)).unwrap();
    assert!(store.window(0, 10).unwrap().outcomes.is_empty());
    assert_eq!(store.window(10, 30).unwrap().outcomes[0].ts, 20);
    assert!(store.append(&outcome(1)).unwrap());
    assert_eq!(store.window(0, 10).unwrap().outcomes.len(), 1);
}
#[test]
fn backfill_twice_no_duplicate() {
    let temp = Temp::new();
    let store = EvidenceStore::open(&temp.0, true).unwrap();
    let EvidenceRow::Outcome(row) = outcome(1) else {
        unreachable!()
    };
    let record = serde_json::json!({"attempts":[{"outcome":row}]});
    assert_eq!(store.backfill(std::slice::from_ref(&record)).unwrap(), 1);
    assert_eq!(store.backfill(&[record]).unwrap(), 0);
    assert!(!store.append(&outcome(1)).unwrap());
    assert_eq!(
        fs::read_to_string(temp.0.join("evidence/rows.jsonl"))
            .unwrap()
            .lines()
            .count(),
        1
    );
}
#[test]
fn window_separates_observations() {
    let temp = Temp::new();
    let store = EvidenceStore::open(&temp.0, true).unwrap();
    store.append(&outcome(1)).unwrap();
    store.append(&observation(1)).unwrap();
    store.append(&observation(2)).unwrap();
    let window = store.window(1, 2).unwrap();
    assert_eq!(window.outcomes.len(), 1);
    assert_eq!(window.observations.len(), 1);
    assert_eq!(window.observations[0].ts, 1);
}
#[cfg(unix)]
#[test]
fn symlink_invalid_complete_line_refuse() {
    let temp = Temp::new();
    let store = EvidenceStore::open(&temp.0, true).unwrap();
    let target = temp.0.join("target");
    fs::write(&target, b"invalid\n").unwrap();
    let rows = temp.0.join("evidence/rows.jsonl");
    std::os::unix::fs::symlink(&target, &rows).unwrap();
    assert!(store.append(&observation(1)).is_err());
    fs::remove_file(&rows).unwrap();
    fs::write(&rows, b"invalid\n").unwrap();
    assert!(store.append(&observation(1)).is_err());
    assert_eq!(fs::read(&rows).unwrap(), b"invalid\n");
}
