//! The rest of the R3b-1 tests (the 400-line gate split them off):
//! `take_promotion`, `decide`'s queue rule, `release` and the aging report.

use super::*;

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
        decide(
            &backfillable,
            &req,
            &c,
            4_096,
            no_rss,
            NOW,
            &AgingPolicy::default()
        ),
        Decision::Grant(_)
    ));
    // ... while an older eligible ticket queues the newcomer ...
    let older_eligible = table(
        vec![],
        vec![ticket("run-a", SlotClass::Other, 100, NOW - 5 * MIN)],
    );
    assert!(matches!(
        decide(
            &older_eligible,
            &req,
            &c,
            4_096,
            no_rss,
            NOW,
            &AgingPolicy::default()
        ),
        Decision::Queue(_)
    ));
    // ... and so does an older reservation (aged 12 min, memory alone).
    let older_reserved = table(
        vec![],
        vec![ticket("run-a", SlotClass::Other, 5_000, NOW - 12 * MIN)],
    );
    assert!(matches!(
        decide(
            &older_reserved,
            &req,
            &c,
            4_096,
            no_rss,
            NOW,
            &AgingPolicy::default()
        ),
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

#[test]
fn decide_and_promote_share_one_aging_ladder() {
    // run-a waits on memory alone for 5 min: under a ladder that reserves
    // from 1 min (not the 10 min default) it is already a reservation.
    let policy = AgingPolicy {
        alert_after_ms: 15 * MIN,
        reserve_after_ms: MIN,
        escalate_after_ms: 20 * MIN,
        release_after_ms: 30 * MIN,
    };
    let c = caps(9, 9, 9, 9, 256);
    let older = ticket("run-a", SlotClass::Other, 5_000, NOW - 5 * MIN);
    // The waiting supervisor promotes itself: it blocks behind the
    // reservation ...
    let waiting = table(
        vec![],
        vec![older.clone(), ticket("run-b", SlotClass::Other, 100, NOW)],
    );
    assert_eq!(
        waited(promote(&waiting, "run-b", &c, 4_096, no_rss, NOW, &policy)),
        WaitReason::Older("run-a".into())
    );
    // ... and a newcomer hits the same blocking rule through decide: queued
    // behind the same reservation, not backfilled around it.
    let newcomer = table(vec![], vec![older]);
    let req = ticket("run-b", SlotClass::Other, 100, NOW);
    assert!(matches!(
        decide(&newcomer, &req, &c, 4_096, no_rss, NOW, &policy),
        Decision::Queue(_)
    ));
}

#[test]
fn a_duplicated_run_id_promotes_and_drops_the_least_key_row() {
    // Two rows for run-a, stored unsorted (the older one second): the row
    // with the least (enqueued_ms, run_id) key is the one to promote.
    let mut t = table(
        vec![],
        vec![
            ticket("run-a", SlotClass::Other, 100, NOW - 2 * MIN),
            ticket("run-a", SlotClass::Other, 50, NOW - 5 * MIN),
        ],
    );
    let c = caps(9, 9, 9, 9, 256);
    // Promoting the younger row instead would wait forever behind the older
    // row of the very same run_id.
    let slot = promoted(promote(
        &t,
        "run-a",
        &c,
        4_096,
        no_rss,
        NOW,
        &AgingPolicy::default(),
    ));
    assert_eq!(slot.reservation_mb, 50, "the row with the least key");
    // take_promotion drops that same row and keeps the other.
    take_promotion(&mut t, slot);
    assert_eq!(
        t.queued,
        vec![ticket("run-a", SlotClass::Other, 100, NOW - 2 * MIN)]
    );
}

#[test]
fn wait_older_names_the_first_of_two_older_blockers() {
    let c = caps(9, 9, 9, 9, 0);
    // run-a (the oldest) and run-b both block run-c, stored newest-first and
    // oldest-first on purpose: the first blocker is the least key either way.
    for queued in [
        vec![
            ticket("run-c", SlotClass::Other, 0, NOW),
            ticket("run-b", SlotClass::Other, 0, NOW - 2 * MIN),
            ticket("run-a", SlotClass::Other, 0, NOW - 5 * MIN),
        ],
        vec![
            ticket("run-c", SlotClass::Other, 0, NOW),
            ticket("run-a", SlotClass::Other, 0, NOW - 5 * MIN),
            ticket("run-b", SlotClass::Other, 0, NOW - 2 * MIN),
        ],
    ] {
        let t = table(vec![], queued);
        assert_eq!(
            waited(promote(
                &t,
                "run-c",
                &c,
                4_096,
                no_rss,
                NOW,
                &AgingPolicy::default(),
            )),
            WaitReason::Older("run-a".into()),
            "the first blocker is the oldest, whatever the file order"
        );
    }
}
