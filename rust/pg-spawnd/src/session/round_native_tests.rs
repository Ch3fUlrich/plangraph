use super::round_native::rows;
use super::round_time::Cause;
use serde_json::json;

fn stage(name: &str, from: i64, to: i64) -> serde_json::Value {
    json!({"action":"stage","stage":name,"started_at_ms":from,"ended_at_ms":to})
}

fn turn(from: i64, to: i64) -> serde_json::Value {
    let stamp = |ms: i64| {
        chrono::DateTime::from_timestamp_millis(ms)
            .unwrap()
            .to_rfc3339()
    };
    json!({"started": stamp(from), "ended": stamp(to)})
}

#[test]
fn each_writer_turn_starts_a_round_and_every_instant_is_booked_once() {
    let base = 1_700_000_000_000;
    let record = json!({"run_id": "run-1", "result": "BLOCKED",
        "stage_timings": [
            stage("writing", base, base + 100_000),
            stage("fast_checks", base + 100_000, base + 120_000),
            stage("review", base + 120_000, base + 200_000),
            // The fix turn: its stage and the final gates overlap by 5 s, which stay with the turn.
            stage("writing", base + 200_000, base + 230_000),
            stage("final_gates", base + 225_000, base + 260_000),
            stage("push", base + 260_000, base + 270_000),
        ],
        "writer_turns": [turn(base, base + 100_000), turn(base + 200_000, base + 230_000)]});
    let rows = rows(&record);
    assert_eq!(rows.len(), 2);
    let ms =
        |row: &super::round_time::RoundRow, cause| row.causes.get(&cause).copied().unwrap_or(0);
    assert_eq!(ms(&rows[0], Cause::NativeTurn), 100_000);
    assert_eq!(ms(&rows[0], Cause::Gate), 20_000);
    assert_eq!(ms(&rows[0], Cause::Review), 80_000);
    assert_eq!(rows[0].wall_ms(), 200_000);
    assert_eq!(ms(&rows[1], Cause::NativeTurn), 30_000);
    assert_eq!(ms(&rows[1], Cause::Gate), 30_000);
    assert_eq!(ms(&rows[1], Cause::Push), 10_000);
    assert_eq!(rows[1].wall_ms(), 70_000);
    assert_eq!(rows[0].round_key, format!("run-1:native:{base}"));
    assert_eq!(
        rows[1].round_key,
        format!("run-1:native:{}", base + 200_000)
    );
    assert_eq!((rows[0].index, rows[1].index), (1, 2));
    assert_eq!(rows[0].outcome, None);
    assert_eq!(rows[1].outcome.as_deref(), Some("BLOCKED"));
    // A re-read of the same record rewrites the same keys.
    assert_eq!(
        rows.iter().map(|r| r.round_key.clone()).collect::<Vec<_>>(),
        super::round_native::rows(&record)
            .iter()
            .map(|r| r.round_key.clone())
            .collect::<Vec<_>>()
    );
}

#[test]
fn a_turn_without_a_stage_fills_only_uncovered_time_and_gaps_are_unknown() {
    let base = 1_700_000_000_000;
    let record = json!({"run_id": "run-3",
        "stage_timings": [stage("push", base + 50_000, base + 60_000)],
        "writer_turns": [turn(base, base + 40_000)]});
    let rows = rows(&record);
    assert_eq!(rows.len(), 1);
    let ms = |cause| rows[0].causes.get(&cause).copied().unwrap_or(0);
    assert_eq!(ms(Cause::NativeTurn), 40_000);
    assert_eq!(ms(Cause::Push), 10_000);
    assert_eq!(ms(Cause::Unknown), 10_000);
}

#[test]
fn a_record_without_stage_intervals_has_no_row() {
    assert!(rows(&json!({"run_id": "run-2", "stage_timings": []})).is_empty());
    assert!(rows(&json!({"run_id": "run-2"})).is_empty());
}

#[test]
fn a_turn_without_a_writing_stage_is_its_own_round() {
    let base = 1_700_000_000_000;
    // Turn one has its stage; turn two (a resume) booked none; turn three has a stage again.
    let record = json!({"run_id": "run-4",
    "stage_timings": [
        stage("writing", base + 10, base + 50_000),
        stage("fast_checks", base + 50_000, base + 60_000),
        stage("writing", base + 200_010, base + 230_000),
    ],
    "writer_turns": [
        turn(base, base + 50_000),
        turn(base + 100_000, base + 160_000),
        turn(base + 200_000, base + 230_000),
    ]});
    let rows = rows(&record);
    assert_eq!(rows.len(), 3, "one round per persisted turn");
    let ms =
        |row: &super::round_time::RoundRow, cause| row.causes.get(&cause).copied().unwrap_or(0);
    assert_eq!(ms(&rows[0], Cause::Gate), 10_000);
    assert_eq!(
        ms(&rows[1], Cause::NativeTurn),
        60_000,
        "the unstaged turn is not folded away"
    );
    assert_eq!(ms(&rows[2], Cause::NativeTurn), 30_000 - 10 + 10);
    assert_eq!(
        rows[1].round_key,
        format!("run-4:native:{}", base + 100_000)
    );
}

#[test]
fn a_record_with_turns_and_no_stage_rows_still_books_its_turns() {
    let base = 1_700_000_000_000;
    let record = json!({"run_id": "run-5",
        "writer_turns": [turn(base, base + 40_000), turn(base + 90_000, base + 120_000)]});
    let rows = rows(&record);
    assert_eq!(rows.len(), 2);
    assert_eq!(rows[0].causes[&Cause::NativeTurn], 40_000);
    assert_eq!(rows[1].causes[&Cause::NativeTurn], 30_000);
}
