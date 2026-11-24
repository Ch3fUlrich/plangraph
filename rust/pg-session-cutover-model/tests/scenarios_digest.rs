//! Scenarios of the digest: scan cursor apart from the ack, byte limit, order, legacy delivery, timers.
mod common;
use common::*;
use pg_session_cutover_model::*;

#[test]
fn a_scan_is_not_a_receipt_and_a_digest_is_bounded_and_replayed_after_a_crash() {
    // gpt-6.2-sol-high round 1 MED, A3 req 6: bounded delivery, scan cursor apart from the ack.
    let mut p = params(vec![URGENT(0), URGENT(0), ORDINARY(0)], 0);
    digest_of(&mut p, 1);
    let mut m = armed_ready(p);
    run(
        &mut m,
        &[
            Event::Occur(0),
            Event::Occur(1),
            Event::Occur(2),
            Event::Scan(0),
        ],
    );
    assert_eq!(
        m.unreceived(),
        3,
        "a scan reads into the digest and acknowledges nothing"
    );
    run(
        &mut m,
        &[Event::WaiterFire(0), Event::BeginTurn(0), Event::Receive(0)],
    );
    // The range is the oldest urgent event first; the rest stays pending.
    assert_eq!(
        (m.stage(0), m.stage(1), m.stage(2)),
        (Stage::Received, Stage::Detected, Stage::Detected)
    );
    assert_eq!(m.apply(Event::EndTurn(0)), Ok(Outcome::Blocked));
    // The session dies before the next receipt: a recovered session replays the remainder.
    run(
        &mut m,
        &[
            Event::Crash(0),
            Event::BeginRecycle,
            Event::Confirm,
            Event::Adopt,
        ],
    );
    assert_eq!((m.stage(1), m.stage(2)), (Stage::Detected, Stage::Detected));
    run(&mut m, &[Event::Receive(1), Event::Receive(1)]);
    assert_eq!((m.stage(1), m.stage(2)), (Stage::Received, Stage::Received));
    // A scan that counted as the receipt would lose an event nobody read.
    let mut bad = armed_ready(params(vec![URGENT(0)], 0)).with_defect(Defect::ScanIsReceipt);
    run(&mut bad, &[Event::Occur(0)]);
    assert_eq!(bad.apply(Event::Scan(0)), Ok(Outcome::Applied));
    assert_eq!(bad.violations(), vec![Invariant::ReceivedUnread]);
}

#[test]
fn timers_beyond_the_counters_are_refused_and_the_largest_runs() {
    // gpt-6.2-sol-high round 1 MED: a 2,550 s scan period overflowed the first shadow tick.
    let with_scan = |secs| {
        let mut p = params(vec![], 0);
        p.scan_secs = secs;
        p
    };
    assert_eq!(Model::new(with_scan(2550)).err(), Some(ParamError::Range));
    assert_eq!(Model::new(with_scan(2010)).err(), Some(ParamError::Range));
    let mut m = model(with_scan(MAX_TICKS * TICK_SECS));
    run(&mut m, &[Event::StartShadow]);
    ticks(&mut m, 300);
    let mut p = params(vec![], 0);
    p.bound_secs = 2550;
    assert_eq!(Model::new(p).err(), Some(ParamError::Range));
    // A digest too small for one event line and its header cannot be built.
    let mut p = params(vec![URGENT(0)], 0);
    p.digest_bytes = HEADER_BYTES + EVENT_BYTES - 1;
    assert_eq!(Model::new(p).err(), Some(ParamError::Range));
    let mut p = params(vec![], 0);
    p.recycle_secs = 2550;
    assert_eq!(Model::new(p).err(), Some(ParamError::Range));
}

#[test]
fn legacy_receipts_after_a_rollback_are_not_capped_by_the_digest() {
    // gpt-6.2-sol-high round 2 HIGH: a backlog larger than the digest, then a rollback.
    let mut p = params(vec![URGENT(0), URGENT(0), URGENT(0)], 0);
    digest_of(&mut p, 1);
    let mut m = armed_ready(p);
    run(
        &mut m,
        &[
            Event::Occur(0),
            Event::Occur(1),
            Event::Occur(2),
            Event::Scan(0),
        ],
    );
    run(
        &mut m,
        &[Event::Rollback, Event::BeginTurn(0), Event::Receive(0)],
    );
    assert!(
        (0..3).all(|e| m.stage(e) == Stage::Received),
        "every message of today's path is read"
    );
    assert_eq!(m.last_digest(), None, "no digest was needed");
    // The digest path of an active role still takes one bounded receipt at a time.
    let mut p = params(vec![URGENT(0), URGENT(0)], 0);
    digest_of(&mut p, 1);
    let mut m = armed_ready(p);
    run(&mut m, &[Event::Occur(0), Event::Occur(1), Event::Scan(0)]);
    run(
        &mut m,
        &[Event::WaiterFire(0), Event::BeginTurn(0), Event::Receive(0)],
    );
    assert_eq!((m.stage(0), m.stage(1)), (Stage::Received, Stage::Detected));
    assert_eq!(
        m.apply(Event::EndTurn(0)),
        Ok(Outcome::Blocked),
        "the remainder holds the turn"
    );
}

#[test]
fn receipts_follow_occurrence_order_not_event_ids() {
    // gpt-6.2-sol-high round 2 MED: a newer event with a lower id must not displace an older one.
    let mut p = params(vec![URGENT(0), URGENT(0)], 0);
    digest_of(&mut p, 1);
    let mut m = armed_ready(p);
    run(&mut m, &[Event::Occur(1), Event::Occur(0), Event::Scan(0)]);
    run(
        &mut m,
        &[Event::WaiterFire(0), Event::BeginTurn(0), Event::Receive(0)],
    );
    assert_eq!((m.stage(1), m.stage(0)), (Stage::Received, Stage::Detected));
    assert_eq!(m.last_digest().map(|d| d.range), Some((1, 1)));
}

#[test]
fn a_digest_starts_with_its_range_and_never_exceeds_its_byte_limit() {
    // gpt-6.2-sol-high round 2 MED, A3 req 6: size bound, range first, nothing dropped.
    let mut p = params(vec![URGENT(0), URGENT(0), URGENT(0)], 0);
    digest_of(&mut p, 2);
    let limit = p.digest_bytes;
    let mut m = armed_ready(p.clone());
    run(
        &mut m,
        &[
            Event::Occur(0),
            Event::Occur(1),
            Event::Occur(2),
            Event::Scan(0),
        ],
    );
    run(
        &mut m,
        &[Event::WaiterFire(0), Event::BeginTurn(0), Event::Receive(0)],
    );
    let digest = m.last_digest().expect("a digest was written");
    assert_eq!(
        (digest.events, digest.bytes, digest.range),
        (2, limit, (1, 2))
    );
    assert!(
        digest.bytes >= HEADER_BYTES + 2 * EVENT_BYTES,
        "the range header is counted"
    );
    assert_eq!(
        m.stage(2),
        Stage::Detected,
        "the rest waits for the next receipt"
    );
    run(&mut m, &[Event::Receive(0)]);
    assert_eq!(
        m.last_digest().map(|d| (d.events, d.range)),
        Some((1, (3, 3)))
    );
    // A digest that ignores its limit pushes the range header past the output cap.
    let mut bad = armed_ready(p).with_defect(Defect::DigestUnbounded);
    run(
        &mut bad,
        &[
            Event::Occur(0),
            Event::Occur(1),
            Event::Occur(2),
            Event::Scan(0),
        ],
    );
    run(&mut bad, &[Event::WaiterFire(0), Event::BeginTurn(0)]);
    assert_eq!(bad.apply(Event::Receive(0)), Ok(Outcome::Applied));
    assert_eq!(bad.violations(), vec![Invariant::DigestOverBudget]);
}
