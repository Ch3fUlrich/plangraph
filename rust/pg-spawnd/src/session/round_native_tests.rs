use super::round_native::row;
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
fn stages_and_turns_book_every_instant_once() {
    let base = 1_700_000_000_000;
    let record = json!({"run_id": "run-1", "result": "BLOCKED",
        "stage_timings": [
            stage("writing", base, base + 100_000),
            stage("fast_checks", base + 100_000, base + 120_000),
            stage("review", base + 120_000, base + 200_000),
            // Final gates overlap the end of review by 5 s: that time stays with review.
            stage("final_gates", base + 195_000, base + 230_000),
            stage("push", base + 230_000, base + 240_000),
        ],
        // The writing stage and its turn are the same interval; a second turn has no stage.
        "writer_turns": [turn(base, base + 100_000), turn(base + 250_000, base + 270_000)]});
    let row = row(&record).unwrap();
    let ms = |cause| row.causes.get(&cause).copied().unwrap_or(0);
    assert_eq!(ms(Cause::NativeTurn), 100_000 + 20_000);
    assert_eq!(ms(Cause::Review), 80_000);
    assert_eq!(ms(Cause::Gate), 20_000 + 30_000);
    assert_eq!(ms(Cause::Push), 10_000);
    assert_eq!(
        ms(Cause::Unknown),
        10_000,
        "the gap between push and the second turn"
    );
    assert_eq!(row.wall_ms(), 270_000);
    assert_eq!(row.wall_ms(), row.ended_ms - row.started_ms);
    assert_eq!(row.round_key, "run-1:native");
    assert_eq!(row.outcome.as_deref(), Some("BLOCKED"));
}

#[test]
fn a_record_without_stage_intervals_has_no_row() {
    assert!(row(&json!({"run_id": "run-2", "stage_timings": []})).is_none());
    assert!(row(&json!({"run_id": "run-2"})).is_none());
}
