use super::{tests::*, *};
use crate::evidence_row::ObservationKind;

fn episode(kind: ObservationKind, phase: Option<Phase>) -> EvidenceRow {
    EvidenceRow::Observation(ObservationRow {
        leg_key: "leg".into(),
        ts: 1,
        kind,
        until: None,
        state: Some("parked".into()),
        first_seen: Some(1),
        phase,
    })
}

fn write_rows(store: &EvidenceStore, rows: &[EvidenceRow]) {
    let bytes: String = rows
        .iter()
        .map(|row| format!("{}\n", json!({"version":1,"data":row})))
        .collect();
    fs::write(store.dir.join("rows.jsonl"), bytes).unwrap();
}

#[test]
fn phase_on_parked_or_stale_refused() {
    let temp = Temp::new();
    let store = EvidenceStore::open(&temp.0, true).unwrap();
    for kind in [
        ObservationKind::OmniRouteParked,
        ObservationKind::OmniRouteStale,
        ObservationKind::Canary,
    ] {
        for phase in [Phase::First, Phase::Cleared] {
            let row = episode(kind.clone(), Some(phase));
            assert!(matches!(store.append(&row), Err(EvidenceError::Invalid)));
            write_rows(&store, &[row]);
            assert!(matches!(store.window(0, 10), Err(EvidenceError::Invalid)));
            assert!(matches!(store.open_episodes(), Err(EvidenceError::Invalid)));
            fs::remove_file(store.dir.join("rows.jsonl")).unwrap();
        }
    }
}

#[test]
fn open_episodes_parked_seen_cleared_stale_leaves_none() {
    use ObservationKind::*;
    let temp = Temp::new();
    let store = EvidenceStore::open(&temp.0, true).unwrap();
    let rows = [
        episode(OmniRouteParked, None),
        episode(OmniRouteSeen, Some(Phase::First)),
        episode(OmniRouteSeen, Some(Phase::Cleared)),
        episode(OmniRouteStale, None),
    ];
    // A legacy phased parked row must not create a phantom episode on restart.
    write_rows(&store, &[episode(OmniRouteParked, Some(Phase::First))]);
    assert!(matches!(store.open_episodes(), Err(EvidenceError::Invalid)));
    // Every stored order: only the relative order of seen phases matters.
    for a in 0..4 {
        for b in 0..4 {
            for c in 0..4 {
                for d in 0..4 {
                    let order = [a, b, c, d];
                    if (0..4).any(|i| order[i + 1..].contains(&order[i])) {
                        continue;
                    }
                    write_rows(&store, &[]);
                    let mut open = false;
                    for i in order {
                        assert!(store.append(&rows[i]).unwrap());
                        assert!(!store.append(&rows[i]).unwrap());
                        if i == 1 || i == 2 {
                            open = i == 1;
                        }
                        let episodes = store.open_episodes().unwrap();
                        assert_eq!(episodes.len(), usize::from(open));
                        if open {
                            assert_eq!(episodes[0].kind, OmniRouteSeen);
                        }
                    }
                    assert_eq!(store.window(0, 10).unwrap().observations.len(), 4);
                }
            }
        }
    }
}

#[test]
fn overlong_line_refused_on_read() {
    let temp = Temp::new();
    let store = EvidenceStore::open(&temp.0, true).unwrap();
    for character in ["x", "界"] {
        let mut value = json!({"version":1,"data":outcome(1)});
        value["data"]["row"]["line"] = json!(character.repeat(300));
        let path = store.dir.join("rows.jsonl");
        fs::write(&path, format!("{value}\n")).unwrap();
        assert_eq!(store.window(0, 10).unwrap().outcomes.len(), 1);
        value["data"]["row"]["line"] = json!(character.repeat(301));
        fs::write(&path, format!("{value}\n")).unwrap();
        assert!(matches!(store.window(0, 10), Err(EvidenceError::Invalid)));
        assert!(matches!(
            store.append(&observation(2)),
            Err(EvidenceError::Invalid)
        ));
    }
}

#[test]
fn existing_rows_without_phase_still_read() {
    let temp = Temp::new();
    let store = EvidenceStore::open(&temp.0, true).unwrap();
    for kind in [
        ObservationKind::OmniRouteParked,
        ObservationKind::OmniRouteStale,
        ObservationKind::Canary,
    ] {
        let mut value = json!({"version":1,"data":episode(kind, None)});
        value["data"]["row"]
            .as_object_mut()
            .unwrap()
            .remove("phase");
        fs::write(store.dir.join("rows.jsonl"), format!("{value}\n")).unwrap();
        assert_eq!(store.window(0, 10).unwrap().observations.len(), 1);
        assert!(store.open_episodes().unwrap().is_empty());
    }
}
