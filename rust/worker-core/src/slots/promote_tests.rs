//! Tests for pull-based promotion, the holder's release and the aging report
//! (R3b-1). Pure and hermetic: an injected `now_ms`, no sleeps, no IO.

use super::policy::{decide, Decision};
use super::promote::{
    aging_report, promote, release, take_promotion, AgingPolicy, AgingReport, Promotion, WaitReason,
};
use super::{ActiveSlot, SlotClass, SlotTable, Ticket};
use crate::hosts::{Caps, Role};

const NOW: u64 = 10_000_000;
const MIN: u64 = 60_000;

fn caps(
    max_runs: u64,
    max_writers: u64,
    max_seats: u64,
    max_sessions: u64,
    mem_floor_mb: u64,
) -> Caps {
    Caps {
        max_runs,
        max_writers,
        max_seats,
        max_sessions,
        mem_floor_mb,
    }
}

fn ticket(run_id: &str, class: SlotClass, reservation_mb: u64, enqueued_ms: u64) -> Ticket {
    Ticket {
        run_id: run_id.into(),
        pid: 11,
        start_time: 1_100,
        enqueued_ms,
        role: Role::Review,
        class,
        repo_id: "repo-b".into(),
        reservation_mb,
    }
}

fn active(run_id: &str, class: SlotClass) -> ActiveSlot {
    ActiveSlot {
        run_id: run_id.into(),
        pid: 10,
        start_time: 1_000,
        role: Role::Implement,
        class,
        repo_id: "repo-a".into(),
        seat_slot: None,
        group: None,
        reservation_mb: 0,
        created_ms: 2_000,
        bound: true,
    }
}

fn active_at(run_id: &str, pid: u32, start_time: u64) -> ActiveSlot {
    let mut slot = active(run_id, SlotClass::Other);
    slot.pid = pid;
    slot.start_time = start_time;
    slot
}

fn table(active: Vec<ActiveSlot>, queued: Vec<Ticket>) -> SlotTable {
    SlotTable {
        version: 1,
        active,
        queued,
    }
}

fn no_rss(_: &ActiveSlot) -> u64 {
    0
}

fn promoted(out: Promotion) -> ActiveSlot {
    match out {
        Promotion::Promoted(slot) => slot,
        other => panic!("expected Promoted, got {other:?}"),
    }
}

fn waited(out: Promotion) -> WaitReason {
    match out {
        Promotion::Wait(reason) => reason,
        other => panic!("expected Wait, got {other:?}"),
    }
}

/// The expected report for one `run-a` ticket, per the four aging rules.
fn report(alert: bool, reserved: bool, escalate: bool, released: bool) -> AgingReport {
    let one = |on: bool| {
        if on {
            vec!["run-a".to_string()]
        } else {
            Vec::new()
        }
    };
    AgingReport {
        alert: one(alert),
        reserved: one(reserved),
        escalate: one(escalate),
        released: one(released),
    }
}

#[test]
fn the_default_ladder_is_15_10_20_30_minutes() {
    let p = AgingPolicy::default();
    assert_eq!(
        (
            p.alert_after_ms,
            p.reserve_after_ms,
            p.escalate_after_ms,
            p.release_after_ms,
        ),
        (900_000, 600_000, 1_200_000, 1_800_000)
    );
}

#[test]
fn the_oldest_eligible_promotes_and_the_younger_waits_with_older() {
    let t = table(
        vec![],
        vec![
            ticket("run-a", SlotClass::Other, 100, NOW - 5 * MIN),
            ticket("run-b", SlotClass::Other, 100, NOW - 2 * MIN),
        ],
    );
    let c = caps(9, 9, 9, 9, 256);
    // The younger eligible ticket waits on the oldest eligible one ...
    assert_eq!(
        waited(promote(
            &t,
            "run-b",
            &c,
            4_096,
            no_rss,
            NOW,
            &AgingPolicy::default()
        )),
        WaitReason::Older("run-a".into())
    );
    // ... and the oldest promotes with a decide-shaped unbound slot.
    let slot = promoted(promote(
        &t,
        "run-a",
        &c,
        4_096,
        no_rss,
        NOW,
        &AgingPolicy::default(),
    ));
    assert_eq!(slot.run_id, "run-a");
    assert_eq!(slot.created_ms, NOW);
    assert_eq!(slot.seat_slot, None);
    assert_eq!(slot.group, None);
    assert!(!slot.bound, "a promoted slot starts unbound");
}

#[test]
fn a_younger_eligible_ticket_backfills_around_a_memory_blocked_one() {
    // run-a waits on memory alone but is younger than 10 min: backfilled
    // around, and it still waits on memory itself.
    let t = table(
        vec![],
        vec![
            ticket("run-a", SlotClass::Other, 5_000, NOW - 5 * MIN),
            ticket("run-b", SlotClass::Other, 100, NOW - 2 * MIN),
        ],
    );
    let c = caps(9, 9, 9, 9, 256);
    assert_eq!(
        waited(promote(
            &t,
            "run-a",
            &c,
            4_096,
            no_rss,
            NOW,
            &AgingPolicy::default()
        )),
        WaitReason::Memory
    );
    assert!(matches!(
        promote(&t, "run-b", &c, 4_096, no_rss, NOW, &AgingPolicy::default()),
        Promotion::Promoted(_)
    ));
}

#[test]
fn no_backfill_around_a_reservation_and_it_promotes_once_memory_frees() {
    // run-a waits on memory alone at 12 min (the 10..30 min window): now a
    // reservation, so nothing younger goes past it.
    let t = table(
        vec![],
        vec![
            ticket("run-a", SlotClass::Other, 5_000, NOW - 12 * MIN),
            ticket("run-b", SlotClass::Other, 100, NOW - 2 * MIN),
        ],
    );
    let c = caps(9, 9, 9, 9, 256);
    assert_eq!(
        waited(promote(
            &t,
            "run-b",
            &c,
            4_096,
            no_rss,
            NOW,
            &AgingPolicy::default()
        )),
        WaitReason::Older("run-a".into())
    );
    assert_eq!(
        waited(promote(
            &t,
            "run-a",
            &c,
            4_096,
            no_rss,
            NOW,
            &AgingPolicy::default()
        )),
        WaitReason::Memory
    );
    // Once the memory is there, the reservation itself promotes.
    let slot = promoted(promote(
        &t,
        "run-a",
        &c,
        8_192,
        no_rss,
        NOW,
        &AgingPolicy::default(),
    ));
    assert_eq!(slot.run_id, "run-a");
}

#[test]
fn a_cap_blocked_ticket_never_becomes_a_reservation() {
    // run-a is a writer at the writer cap: it waits on its class cap alone,
    // whatever its age, so a younger class with room backfills around it.
    let t = table(
        vec![active("run-x", SlotClass::Writer)],
        vec![
            ticket("run-a", SlotClass::Writer, 0, NOW - 12 * MIN),
            ticket("run-b", SlotClass::Session, 0, NOW - 2 * MIN),
        ],
    );
    let c = caps(9, 1, 9, 2, 0);
    assert_eq!(
        waited(promote(
            &t,
            "run-a",
            &c,
            4_096,
            no_rss,
            NOW,
            &AgingPolicy::default()
        )),
        WaitReason::Caps
    );
    assert!(matches!(
        promote(&t, "run-b", &c, 4_096, no_rss, NOW, &AgingPolicy::default()),
        Promotion::Promoted(_)
    ));
    // The report agrees: waiting on its cap, it is not a reservation.
    let r = aging_report(&t, NOW, &AgingPolicy::default(), &c, 4_096, no_rss);
    assert_eq!(r.reserved, Vec::<String>::new());
}

#[test]
fn a_ticket_past_the_release_age_blocks_nothing() {
    // run-a waits on memory alone for 31 min: released, so backfill goes on.
    let t = table(
        vec![],
        vec![
            ticket("run-a", SlotClass::Other, 5_000, NOW - 31 * MIN),
            ticket("run-b", SlotClass::Other, 100, NOW - 2 * MIN),
        ],
    );
    let c = caps(9, 9, 9, 9, 256);
    assert!(matches!(
        promote(&t, "run-b", &c, 4_096, no_rss, NOW, &AgingPolicy::default()),
        Promotion::Promoted(_)
    ));
    let r = aging_report(&t, NOW, &AgingPolicy::default(), &c, 4_096, no_rss);
    assert_eq!(
        r.reserved,
        Vec::<String>::new(),
        "past 30 min nothing reserves"
    );
    assert_eq!(r.released, vec!["run-a".to_string()]);
}

#[test]
fn ties_are_broken_by_run_id_in_the_total_order() {
    // Same stamp: "run-a" sorts before "run-b" and blocks it. Stored in the
    // opposite order on purpose: the order is the key, not the file.
    let t = table(
        vec![],
        vec![
            ticket("run-b", SlotClass::Other, 0, NOW - 5 * MIN),
            ticket("run-a", SlotClass::Other, 0, NOW - 5 * MIN),
        ],
    );
    let c = caps(9, 9, 9, 9, 0);
    assert_eq!(
        waited(promote(
            &t,
            "run-b",
            &c,
            4_096,
            no_rss,
            NOW,
            &AgingPolicy::default()
        )),
        WaitReason::Older("run-a".into())
    );
    assert!(matches!(
        promote(&t, "run-a", &c, 4_096, no_rss, NOW, &AgingPolicy::default()),
        Promotion::Promoted(_)
    ));
}

#[test]
fn a_run_id_that_is_not_queued_is_not_queued() {
    let t = table(
        vec![active("run-live", SlotClass::Other)],
        vec![ticket("run-a", SlotClass::Other, 0, NOW - 5 * MIN)],
    );
    let c = caps(9, 9, 9, 9, 0);
    // An active run holds no ticket ...
    assert_eq!(
        promote(
            &t,
            "run-live",
            &c,
            4_096,
            no_rss,
            NOW,
            &AgingPolicy::default()
        ),
        Promotion::NotQueued
    );
    // ... and neither does an unknown run_id.
    assert_eq!(
        promote(
            &t,
            "run-zz",
            &c,
            4_096,
            no_rss,
            NOW,
            &AgingPolicy::default()
        ),
        Promotion::NotQueued
    );
}

#[test]
fn take_promotion_drops_the_ticket_and_keeps_the_others_in_order() {
    let mut t = table(
        vec![],
        vec![
            ticket("run-a", SlotClass::Other, 0, NOW - 5 * MIN),
            ticket("run-b", SlotClass::Other, 0, NOW - 3 * MIN),
            ticket("run-c", SlotClass::Other, 0, NOW - MIN),
        ],
    );
    let slot = ActiveSlot {
        run_id: "run-b".into(),
        pid: 11,
        start_time: 1_100,
        role: Role::Review,
        class: SlotClass::Other,
        repo_id: "repo-b".into(),
        seat_slot: None,
        group: None,
        reservation_mb: 0,
        created_ms: NOW,
        bound: false,
    };
    take_promotion(&mut t, slot.clone());
    assert_eq!(
        t.queued,
        vec![
            ticket("run-a", SlotClass::Other, 0, NOW - 5 * MIN),
            ticket("run-c", SlotClass::Other, 0, NOW - MIN),
        ]
    );
    assert_eq!(t.active, vec![slot]);
}

#[test]
fn decide_backfills_and_queues_by_the_same_blocking_rule() {
    let c = caps(9, 9, 9, 9, 256);
    let req = ticket("run-b", SlotClass::Other, 100, NOW);
    // An older ticket that is neither eligible nor a reservation is
    // backfilled around ...
    let backfillable = table(
        vec![],
        vec![ticket("run-a", SlotClass::Other, 5_000, NOW - 5 * MIN)],
    );
    assert!(matches!(
        decide(&backfillable, &req, &c, 4_096, no_rss, NOW),
        Decision::Grant(_)
    ));
    // ... while an older eligible ticket queues the newcomer ...
    let older_eligible = table(
        vec![],
        vec![ticket("run-a", SlotClass::Other, 100, NOW - 5 * MIN)],
    );
    assert!(matches!(
        decide(&older_eligible, &req, &c, 4_096, no_rss, NOW),
        Decision::Queue(_)
    ));
    // ... and so does an older reservation (aged 12 min, memory alone).
    let older_reserved = table(
        vec![],
        vec![ticket("run-a", SlotClass::Other, 5_000, NOW - 12 * MIN)],
    );
    assert!(matches!(
        decide(&older_reserved, &req, &c, 4_096, no_rss, NOW),
        Decision::Queue(_)
    ));
}

#[test]
fn release_removes_only_the_exact_run_pid_start_time_row() {
    let mut t = table(
        vec![
            active_at("run-a", 10, 1_000),
            active_at("run-a", 11, 1_000),
            active_at("run-a", 10, 2_000),
        ],
        vec![ticket("run-b", SlotClass::Other, 0, NOW)],
    );
    // A near miss (wrong start_time) removes nothing ...
    assert!(!release(&mut t, "run-a", 10, 9_999));
    // ... the exact row goes ...
    assert!(release(&mut t, "run-a", 10, 1_000));
    assert_eq!(
        t.active
            .iter()
            .map(|s| (s.pid, s.start_time))
            .collect::<Vec<_>>(),
        vec![(11, 1_000), (10, 2_000)],
        "the other rows survive, in order"
    );
    // ... and only once: a pid reused by another attempt keeps its slot.
    assert!(!release(&mut t, "run-a", 10, 1_000), "already gone");
    assert!(
        !release(&mut t, "run-b", 11, 1_100),
        "queued tickets hold no slot"
    );
    assert_eq!(t.queued.len(), 1, "release never touches the queue");
}

#[test]
fn aging_report_walks_the_aging_boundaries() {
    let c = caps(9, 9, 9, 9, 256);
    let policy = AgingPolicy::default();
    // One run-a ticket waiting on memory alone; every row is
    // (alert, reserved, escalate, released) at that age.
    for (age_min, want) in [
        (14, report(false, true, false, false)),
        (15, report(true, true, false, false)),
        (19, report(true, true, false, false)),
        (20, report(true, true, true, false)),
        (29, report(true, true, true, false)),
        (30, report(true, false, true, true)),
    ] {
        let t = table(
            vec![],
            vec![ticket(
                "run-a",
                SlotClass::Other,
                5_000,
                NOW - age_min * MIN,
            )],
        );
        let got = aging_report(&t, NOW, &policy, &c, 4_096, no_rss);
        assert_eq!(got, want, "at {age_min} min");
    }
    // Escalation is for unplaceable tickets only: an eligible one never
    // escalates, however old.
    let eligible_old = table(
        vec![],
        vec![ticket("run-a", SlotClass::Other, 0, NOW - 25 * MIN)],
    );
    let r = aging_report(&eligible_old, NOW, &policy, &c, 4_096, no_rss);
    assert_eq!(r.escalate, Vec::<String>::new());
}

#[test]
fn aging_report_lists_run_ids_in_queue_order() {
    let c = caps(9, 9, 9, 9, 256);
    // Stored out of order on purpose: the order is (enqueued_ms, run_id).
    let t = table(
        vec![],
        vec![
            ticket("run-b", SlotClass::Other, 5_000, NOW - 20 * MIN),
            ticket("run-a", SlotClass::Other, 5_000, NOW - 20 * MIN),
            ticket("run-c", SlotClass::Other, 5_000, NOW - 16 * MIN),
        ],
    );
    let r = aging_report(&t, NOW, &AgingPolicy::default(), &c, 4_096, no_rss);
    assert_eq!(r.alert, vec!["run-a", "run-b", "run-c"]);
}
