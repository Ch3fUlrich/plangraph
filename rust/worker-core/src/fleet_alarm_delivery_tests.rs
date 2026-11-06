use super::*;

#[test]
fn fleet_stale_heartbeat_does_not_rearm() {
    let mut monitor = HeartbeatMonitor::new("test".into(), 0, 10, 1, 1).unwrap();
    let mut sent = Vec::new();
    let mut sink = |id: &str| {
        sent.push(id.to_owned());
        Ok::<_, ()>(())
    };
    monitor.tick(None, 25, &mut sink);
    monitor.tick(Some(15), 25, &mut sink);
    assert_eq!(monitor.last_heartbeat, 15);
    assert_eq!(sent, ["test:silence:1"]);
}

fn restore<T: Serialize + serde::de::DeserializeOwned>(value: &T) -> T {
    serde_json::from_slice(&serde_json::to_vec(value).unwrap()).unwrap()
}

#[test]
fn dropped_transport_explicit_ack_episode_and_restore() {
    let mut state = Deliveries::new(5, 3).unwrap();
    let mut sent = Vec::new();
    let mut drop = |id: &str| {
        sent.push(id.to_owned());
        Err::<(), _>("dropped")
    };
    assert!(!state.acknowledge("load:1"));
    assert!(state.tick("load:1", 0, &mut drop));
    assert_eq!(state.record("load:1").unwrap().attempts, 1);
    assert!(!state.tick("load:1", 4, &mut drop));
    state = restore(&state);
    assert!(state.tick("load:1", 5, &mut drop));
    assert!(!state.acknowledge("wrong"));
    let mut success = |_: &str| Ok::<_, ()>(());
    assert!(state.tick("load:1", 10, &mut success));
    let record = state.record("load:1").unwrap();
    assert_eq!(record.attempts, 3);
    assert!(!record.acknowledged);
    assert_eq!(record.next_retry, None);
    state = restore(&state);
    assert!(!state.tick("load:1", 100, &mut success));
    assert!(state.acknowledge("load:1"));
    assert!(!state.acknowledge("load:1"));
    // Recovery/re-entry uses a new stable episode ID, never resets the old one.
    assert!(state.tick("load:2", 100, &mut success));
    assert!(!state.acknowledge("load:1"));
    assert!(!state.record("load:2").unwrap().acknowledged);
    assert!(state.acknowledge("load:2"));
    state = restore(&state);
    assert!(!state.tick("load:2", 200, &mut success));
    assert_eq!(sent, ["load:1", "load:1"]);
    assert!(Deliveries::new(0, 1).is_err());
    assert!(Deliveries::new(1, 0).is_err());
}

#[test]
fn independent_silence_boundary_recovery_and_bounded_secondary() {
    let mut primary = Deliveries::new(1, 1).unwrap();
    let mut drop = |_: &str| Err::<(), _>(());
    assert!(primary.tick("load:1", 0, &mut drop));
    assert!(!primary.tick("load:1", 9, &mut drop));
    let mut monitor = HeartbeatMonitor::new("operator".into(), 0, 10, 2, 2).unwrap();
    assert_eq!(monitor.tick(None, 9, &mut drop), Findings::default());
    let first = monitor.tick(None, 10, &mut drop).silence_id.unwrap();
    assert_eq!(monitor.delivery.record(&first).unwrap().attempts, 1);
    assert!(primary.acknowledge("load:1"));
    monitor = restore(&monitor);
    for now in [10, 11, 12, 20, 30] {
        assert_eq!(monitor.tick(None, now, &mut drop), Findings::default());
    }
    assert_eq!(monitor.delivery.record(&first).unwrap().attempts, 2);
    assert!(monitor.delivery.acknowledge(&first));
    assert_eq!(monitor.tick(Some(31), 31, &mut drop), Findings::default());
    assert_eq!(monitor.tick(Some(31), 40, &mut drop), Findings::default());
    let second = monitor.tick(Some(31), 41, &mut drop).silence_id.unwrap();
    assert_ne!(first, second);
    assert!(!monitor.delivery.acknowledge(&first));
    assert!(!monitor.delivery.record(&second).unwrap().acknowledged);
    for now in 42..50 {
        assert_eq!(monitor.tick(Some(31), now, &mut drop), Findings::default());
    }
    assert_eq!(monitor.delivery.record(&second).unwrap().attempts, 2);
}

#[test]
fn clock_faults_do_not_recover_or_suppress_due_silence() {
    let mut m = HeartbeatMonitor::new("operator".into(), 0, 10, 2, 2).unwrap();
    let mut sink = |_: &str| Ok::<_, ()>(());
    m.tick(Some(0), 9, &mut sink);
    let before = serde_json::to_value(&m).unwrap();
    assert!(m.tick(Some(1), 8, &mut sink).clock_discontinuity);
    assert_eq!(serde_json::to_value(&m).unwrap(), before);
    let finding = m.tick(Some(100), 10, &mut sink);
    assert!(finding.clock_discontinuity);
    let id = finding.silence_id.unwrap();
    assert!(m.tick(Some(100), 12, &mut sink).silence_id.is_none());
    assert_eq!(m.delivery.record(&id).unwrap().attempts, 2);
    m.tick(Some(13), 13, &mut sink);
    let finding = m.tick(Some(0), 23, &mut sink);
    assert!(finding.clock_discontinuity);
    assert_ne!(finding.silence_id.unwrap(), id);
}
