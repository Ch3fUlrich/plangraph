use super::policy::{class_counts, ClassCounts};
use super::*;
use crate::hosts::Caps;
use std::time::Duration;

const DEADLINE: Duration = Duration::from_secs(2);

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

fn slot(class: SlotClass, reservation_mb: u64) -> ActiveSlot {
    ActiveSlot {
        run_id: "run-a".into(),
        pid: 10,
        start_time: 1_000,
        role: Role::Implement,
        class,
        repo_id: "repo-a".into(),
        seat_slot: None,
        group: None,
        reservation_mb,
        created_ms: 2_000,
        bound: true,
    }
}

fn ticket(class: SlotClass, reservation_mb: u64, enqueued_ms: u64) -> Ticket {
    Ticket {
        run_id: "run-b".into(),
        pid: 11,
        start_time: 1_100,
        enqueued_ms,
        role: Role::Review,
        class,
        repo_id: "repo-b".into(),
        reservation_mb,
    }
}

fn no_rss(_: &ActiveSlot) -> u64 {
    0
}

fn grant(d: Decision) -> ActiveSlot {
    match d {
        Decision::Grant(s) => s,
        Decision::Queue(_) => panic!("expected Grant, got Queue"),
    }
}

fn queue(d: Decision) -> Ticket {
    match d {
        Decision::Queue(t) => t,
        Decision::Grant(_) => panic!("expected Queue, got Grant"),
    }
}

#[test]
fn with_table_round_trips_and_saves_only_changes() {
    let dir = tempfile::tempdir().unwrap();
    let out = with_table(dir.path(), DEADLINE, |t| {
        t.active.push(slot(SlotClass::Writer, 100));
        t.queued.push(ticket(SlotClass::Seat, 50, 1_000));
        "done"
    })
    .unwrap();
    assert_eq!(out, "done");
    let text = std::fs::read_to_string(io::table_path(dir.path())).unwrap();
    assert!(text.contains("\"run_id\""), "snake_case fields");
    assert!(text.contains("\"writer\""), "snake_case class values");
    let seen = with_table(dir.path(), DEADLINE, |t| t.clone()).unwrap();
    assert_eq!(
        seen,
        SlotTable {
            version: 1,
            active: vec![slot(SlotClass::Writer, 100)],
            queued: vec![ticket(SlotClass::Seat, 50, 1_000)],
        }
    );
    // An unchanged table is not saved at all.
    std::fs::remove_file(io::table_path(dir.path())).unwrap();
    with_table(dir.path(), DEADLINE, |_| ()).unwrap();
    assert!(!io::table_path(dir.path()).exists());
}

#[test]
fn missing_file_is_an_empty_table() {
    let dir = tempfile::tempdir().unwrap();
    let seen = with_table(dir.path(), DEADLINE, |t| t.clone()).unwrap();
    assert_eq!(seen, SlotTable::default());
    assert_eq!(seen.version, 1);
    assert!(!io::table_path(dir.path()).exists());
}

#[test]
fn unknown_field_is_refused() {
    let dir = tempfile::tempdir().unwrap();
    std::fs::write(
        io::table_path(dir.path()),
        r#"{"version":1,"active":[],"queued":[],"bogus":1}"#,
    )
    .unwrap();
    let err = with_table(dir.path(), DEADLINE, |_| ()).unwrap_err();
    assert!(matches!(err, SlotError::Parse(_)), "{err:?}");
}

#[test]
fn version_other_than_one_is_refused() {
    let dir = tempfile::tempdir().unwrap();
    std::fs::write(
        io::table_path(dir.path()),
        r#"{"version":2,"active":[],"queued":[]}"#,
    )
    .unwrap();
    let err = with_table(dir.path(), DEADLINE, |_| ()).unwrap_err();
    assert!(matches!(err, SlotError::Version(2)), "{err:?}");
}

#[test]
fn crash_between_temp_write_and_rename_keeps_old_table() {
    let dir = tempfile::tempdir().unwrap();
    with_table(dir.path(), DEADLINE, |t| {
        t.active.push(slot(SlotClass::Writer, 100));
    })
    .unwrap();
    let before = std::fs::read(io::table_path(dir.path())).unwrap();
    // Simulated crash: the temp file lands but the rename never happens.
    let tmp = io::write_temp(dir.path(), &SlotTable::default()).unwrap();
    assert!(tmp.exists());
    let after = std::fs::read(io::table_path(dir.path())).unwrap();
    assert_eq!(before, after, "old slots.json must survive intact");
    let seen = io::load(&io::table_path(dir.path())).unwrap();
    assert_eq!(seen.active, vec![slot(SlotClass::Writer, 100)]);
}

#[test]
fn effective_free_subtracts_only_unmaterialized_reservation() {
    let below = slot(SlotClass::Writer, 100); // rss 40 -> 60 still promised
    let equal = slot(SlotClass::Seat, 200); // rss 200 -> nothing promised
    let above = slot(SlotClass::Other, 50); // rss 80 -> rss already overshoots
    let active = vec![below, equal, above];
    let rss = |s: &ActiveSlot| match s.class {
        SlotClass::Writer => 40,
        SlotClass::Seat => 200,
        _ => 80,
    };
    assert_eq!(effective_free_mb(1_000, &active, rss), 940);
    assert_eq!(effective_free_mb(1_000, &[], rss), 1_000);
    // Saturating at 0 when the promises outrun what is available.
    assert_eq!(effective_free_mb(30, &active, rss), 0);
}

#[test]
fn caps_writer_counts_writer_and_run_caps() {
    let one_writer = SlotTable {
        version: 1,
        active: vec![slot(SlotClass::Writer, 0)],
        queued: vec![],
    };
    // The writer cap is full even though the run cap has room ...
    let c = caps(3, 1, 3, 3, 0);
    let req = ticket(SlotClass::Writer, 0, 1);
    assert_eq!(
        queue(decide(
            &one_writer,
            &req,
            &c,
            9_999,
            no_rss,
            2,
            &AgingPolicy::default()
        )),
        req
    );
    // ... and a seat slot still fits under both its caps.
    grant(decide(
        &one_writer,
        &ticket(SlotClass::Seat, 0, 1),
        &c,
        9_999,
        no_rss,
        2,
        &AgingPolicy::default(),
    ));
    // With max_runs 1 the same writer has also spent the run cap.
    let c = caps(1, 5, 5, 5, 0);
    queue(decide(
        &one_writer,
        &ticket(SlotClass::Other, 0, 1),
        &c,
        9_999,
        no_rss,
        2,
        &AgingPolicy::default(),
    ));
}

#[test]
fn caps_session_counts_only_the_session_cap() {
    let two_sessions = SlotTable {
        version: 1,
        active: vec![slot(SlotClass::Session, 0), slot(SlotClass::Session, 0)],
        queued: vec![],
    };
    let c = caps(1, 1, 1, 2, 0);
    // The session cap is full ...
    queue(decide(
        &two_sessions,
        &ticket(SlotClass::Session, 0, 1),
        &c,
        9_999,
        no_rss,
        2,
        &AgingPolicy::default(),
    ));
    // ... but the sessions hold no run, writer or seat cap at all.
    grant(decide(
        &two_sessions,
        &ticket(SlotClass::Writer, 0, 1),
        &c,
        9_999,
        no_rss,
        2,
        &AgingPolicy::default(),
    ));
    // The counters agree with the same picture.
    assert_eq!(
        class_counts(&two_sessions.active),
        ClassCounts {
            runs: 0,
            writers: 0,
            seats: 0,
            sessions: 2,
        }
    );
}

#[test]
fn decide_grants_an_unbound_slot() {
    let req = ticket(SlotClass::Seat, 100, 500);
    let slot = grant(decide(
        &SlotTable::default(),
        &req,
        &caps(2, 2, 2, 2, 128),
        4_096,
        no_rss,
        7_000,
        &AgingPolicy::default(),
    ));
    assert_eq!(slot.run_id, req.run_id);
    assert_eq!(slot.pid, req.pid);
    assert_eq!(slot.start_time, req.start_time);
    assert_eq!(slot.role, req.role);
    assert_eq!(slot.class, req.class);
    assert_eq!(slot.repo_id, req.repo_id);
    assert_eq!(slot.reservation_mb, req.reservation_mb);
    assert_eq!(slot.created_ms, 7_000);
    assert_eq!(slot.seat_slot, None);
    assert_eq!(slot.group, None);
    assert!(!slot.bound, "a granted slot starts unbound");
}

#[test]
fn decide_queues_on_caps() {
    let table = SlotTable {
        version: 1,
        active: vec![slot(SlotClass::Writer, 0)],
        queued: vec![],
    };
    let req = ticket(SlotClass::Writer, 0, 500);
    let got = queue(decide(
        &table,
        &req,
        &caps(9, 1, 9, 9, 0),
        9_999,
        no_rss,
        700,
        &AgingPolicy::default(),
    ));
    assert_eq!(got, req, "a queued ticket is the request, stamp included");
}

#[test]
fn decide_queues_on_the_memory_floor() {
    let req = ticket(SlotClass::Other, 300, 500);
    // 512 free minus a 300 MB reservation leaves 212, below the 256 floor.
    queue(decide(
        &SlotTable::default(),
        &req,
        &caps(9, 9, 9, 9, 256),
        512,
        no_rss,
        700,
        &AgingPolicy::default(),
    ));
    // The same request passes with a smaller reservation (312 >= 256).
    grant(decide(
        &SlotTable::default(),
        &ticket(SlotClass::Other, 200, 500),
        &caps(9, 9, 9, 9, 256),
        512,
        no_rss,
        700,
        &AgingPolicy::default(),
    ));
}

#[test]
fn decide_queues_behind_an_older_ticket() {
    let table = SlotTable {
        version: 1,
        active: vec![],
        queued: vec![ticket(SlotClass::Writer, 50, 500)],
    };
    let c = caps(9, 9, 9, 9, 0);
    // Caps and memory pass, but an older ticket waits: FIFO across classes.
    queue(decide(
        &table,
        &ticket(SlotClass::Session, 50, 1_000),
        &c,
        9_999,
        no_rss,
        1_000,
        &AgingPolicy::default(),
    ));
    // A request older than everything queued takes the slot.
    grant(decide(
        &table,
        &ticket(SlotClass::Session, 50, 300),
        &c,
        9_999,
        no_rss,
        1_000,
        &AgingPolicy::default(),
    ));
}

#[test]
fn decide_queues_a_reservation_above_free_memory_with_a_zero_floor() {
    // 100 free and a 300 MB reservation: 100 - 300 < 0 even when the floor is 0.
    queue(decide(
        &SlotTable::default(),
        &ticket(SlotClass::Other, 300, 500),
        &caps(9, 9, 9, 9, 0),
        100,
        no_rss,
        700,
        &AgingPolicy::default(),
    ));
    // Exactly the free memory passes a zero floor.
    grant(decide(
        &SlotTable::default(),
        &ticket(SlotClass::Other, 100, 500),
        &caps(9, 9, 9, 9, 0),
        100,
        no_rss,
        700,
        &AgingPolicy::default(),
    ));
}

#[test]
fn decide_a_queued_ticket_with_the_same_stamp_does_not_block() {
    // The requester's own queued ticket (same enqueued_ms) never blocks it.
    let req = ticket(SlotClass::Writer, 50, 500);
    let table = SlotTable {
        version: 1,
        active: vec![],
        queued: vec![req.clone()],
    };
    grant(decide(
        &table,
        &req,
        &caps(9, 9, 9, 9, 0),
        9_999,
        no_rss,
        1_000,
        &AgingPolicy::default(),
    ));
}
