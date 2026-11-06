use super::*;
use crate::fleet_stale_pair_alert::{Clock, Member, Snapshot, State};

fn config() -> Config {
    Config {
        maximum_age: 10,
        divergence_tolerance: 2,
        compaction_cap: 3,
        silence_deadline: 10,
        retry_delay: 2,
        maximum_attempts: 2,
    }
}

fn episode() -> Finding {
    let bytes = serde_json::to_vec(&Snapshot {
        members: ["orchard", "lighthouse"].map(|identity| Member {
            identity: identity.into(),
            revision: "r1".into(),
            checkpoint: "c1".into(),
            freshness: Some(0),
            provenance: "fixture".into(),
        }),
        compactions: Some(0),
    })
    .unwrap();
    let mut state = State::default();
    let mut observe = |t| {
        state.observe(
            &config(),
            &bytes,
            Clock {
                wall: t as i64,
                monotonic: t,
                discontinuity: None,
            },
        )
    };
    observe(0);
    let finding = observe(10);
    assert!(finding.new_alarm);
    assert_eq!(finding.elapsed_compactions, Some(0));
    finding
}

fn restore(delivery: &Delivery) -> Delivery {
    serde_json::from_slice(&serde_json::to_vec(delivery).unwrap()).unwrap()
}

#[test]
fn direct_delivery_ack_retry_and_restore() {
    for transport_success in [false, true] {
        let mut delivery = Delivery::new("pair".into(), 0, &config()).unwrap();
        let finding = episode();
        let mut sends = Vec::new();
        let mut sink = |id: &str| {
            sends.push(id.to_owned());
            if transport_success {
                Ok(())
            } else {
                Err(())
            }
        };
        assert!(delivery.tick(Some(&finding), None, 10, &mut sink));
        let id = "pair:pair:1";
        let record = delivery.primary.record(id).unwrap();
        assert_eq!(record.attempts, 1);
        assert!(!record.acknowledged);
        assert_eq!(record.next_retry, Some(12));
        delivery = restore(&delivery);
        assert!(!delivery.tick(Some(&finding), Some("wrong"), 11, &mut sink));
        assert!(delivery.tick(Some(&finding), Some("pair:pair:0"), 12, &mut sink));
        delivery = restore(&delivery);
        assert!(!delivery.tick(Some(&finding), None, 100, &mut sink));
        assert_eq!(sends, [id, id]);
        assert_eq!(delivery.primary.record(id).unwrap().attempts, 2);
        assert_eq!(delivery.primary.record(id).unwrap().next_retry, None);
        let mut next = finding.clone();
        next.episode = Some(2);
        assert!(delivery.tick(Some(&next), Some(id), 101, &mut |_| Ok::<_, ()>(())));
        assert!(!delivery.primary.record("pair:pair:2").unwrap().acknowledged);
        assert!(!delivery.tick(None, Some("pair:pair:2"), 103, &mut |_| Ok::<_, ()>(())));
        assert!(delivery.primary.record("pair:pair:2").unwrap().acknowledged);
        assert!(!delivery.tick(Some(&finding), None, 200, &mut |_| Ok::<_, ()>(())));
    }
    let mut c = config();
    c.maximum_attempts = 0;
    assert!(Delivery::new("pair".into(), 0, &c).is_err());
    c.maximum_attempts = u64::MAX;
    assert!(Delivery::new("pair".into(), 0, &c).is_err());
}

#[test]
fn independent_silence_without_samples_or_primary_budget() {
    let mut delivery = Delivery::new("pair".into(), 0, &config()).unwrap();
    let finding = episode();
    let mut dropped = |_: &str| Err::<(), _>(());
    assert!(delivery.tick(Some(&finding), None, 0, &mut dropped));
    assert!(delivery.tick(None, None, 2, &mut dropped));
    assert!(!delivery.tick(None, Some("pair:pair:1"), 9, &mut dropped));
    let mut sends = Vec::new();
    let mut secondary = |id: &str| {
        sends.push(id.to_owned());
        Ok::<_, ()>(())
    };
    assert_eq!(
        delivery.silence.tick(None, 9, &mut secondary).silence_id,
        None
    );
    let id = "pair:heartbeat:silence:1";
    assert_eq!(
        delivery
            .silence
            .tick(None, 10, &mut secondary)
            .silence_id
            .as_deref(),
        Some(id)
    );
    delivery = restore(&delivery);
    for t in [11, 12, 50] {
        assert_eq!(
            delivery.silence.tick(None, t, &mut secondary).silence_id,
            None
        );
    }
    assert_eq!(delivery.silence.delivery.record(id).unwrap().attempts, 2);
    assert!(!delivery.silence.delivery.record(id).unwrap().acknowledged);
    assert!(delivery.silence.delivery.acknowledge(id));
    assert!(
        delivery
            .silence
            .tick(Some(60), 51, &mut secondary)
            .clock_discontinuity
    );
    assert_eq!(
        delivery.silence.tick(None, 52, &mut secondary).silence_id,
        None
    );
    assert_eq!(
        delivery
            .silence
            .tick(Some(53), 53, &mut secondary)
            .silence_id,
        None
    );
    assert_eq!(
        delivery
            .silence
            .tick(Some(53), 62, &mut secondary)
            .silence_id,
        None
    );
    assert_eq!(
        delivery
            .silence
            .tick(Some(53), 63, &mut secondary)
            .silence_id
            .as_deref(),
        Some("pair:heartbeat:silence:2")
    );
    assert_eq!(sends, [id, id, "pair:heartbeat:silence:2"]);
}
