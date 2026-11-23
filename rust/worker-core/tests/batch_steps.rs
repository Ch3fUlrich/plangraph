use pg_worker_core::batch_steps::BatchSteps;
use serde_json::{json, Value};

const HEAD: &str = "1111111111111111111111111111111111111111";
const FIXED: &str = "2222222222222222222222222222222222222222";

fn sample() -> Value {
    let steps = [
        json!({"type":"claim","items":["demo:EXAMPLE"],"result":{"status":"completed","output":{"claim":"claim-1"}}}),
        json!({"type":"worktree","claim":"claim-1","base":HEAD,"result":{"status":"completed","output":{"worktree":"worktree-1"}}}),
        json!({"type":"write","worktree":"worktree-1","brief":"brief-1","result":{"status":"completed","output":{"head":HEAD,"session":"session-1"}}}),
        json!({"type":"fast_checks","head":HEAD,"result":{"status":"completed","output":{"stamp":"stamp-1"}}}),
        json!({"type":"review","head":HEAD,"stamp":"stamp-1","result":{"status":"completed","output":{"verdict":"request_changes","review":"review-1"}}}),
        json!({"type":"fix","head":HEAD,"session":"session-1","review":"review-1","result":{"status":"completed","output":{"head":FIXED}}}),
        json!({"type":"fast_checks","head":FIXED,"result":{"status":"completed","output":{"stamp":"stamp-2"}}}),
        json!({"type":"review","head":FIXED,"stamp":"stamp-2","result":{"status":"completed","output":{"verdict":"accept","review":"review-2"}}}),
        json!({"type":"push","head":FIXED,"stamp":"stamp-2","review":"review-2","result":{"status":"completed","output":{"pr":123}}}),
        json!({"type":"ci_class","head":FIXED,"pr":123,"result":{"status":"completed","output":{"classification":"green","run":456,"attempt":1}}}),
        json!({"type":"train_join","head":FIXED,"pr":123,"run":456,"attempt":1,"result":{"status":"completed","output":{"train":"train-1","manifest":"manifest-1"}}}),
    ];
    let rows: Vec<_> = steps
        .into_iter()
        .enumerate()
        .map(|(index, step)| {
            json!({
                "step": step,
                "stage": {
                    "step_index": index,
                    "attempt": 1,
                    "started_at_ms": 1000 + index * 200,
                    "ended_at_ms": 1100 + index * 200
                }
            })
        })
        .collect();
    json!({"version":1,"repo":"demo","batch_key":"demo-batch","rows":rows})
}

fn validate(value: Value) -> Result<(), String> {
    serde_json::from_value::<BatchSteps>(value)
        .map_err(|err| err.to_string())?
        .validate()
}

fn add_failed_attempt(example: &mut Value, index: usize, reason: &str) {
    let rows = example["rows"].as_array_mut().unwrap();
    let mut failed = rows[index].clone();
    failed["step"]["result"] = json!({"status":"failed","reason":reason});
    rows[index]["stage"]["attempt"] = json!(2);
    rows[index]["stage"]["started_at_ms"] = failed["stage"]["ended_at_ms"].clone();
    rows[index]["stage"]["ended_at_ms"] = json!(1200 + index * 200);
    rows.insert(index, failed);
}

#[test]
fn sample_batch_validates_all_step_types_through_train_join() {
    let example = sample();
    let batch = serde_json::from_value::<BatchSteps>(example.clone()).unwrap();
    batch.validate().unwrap();
    assert_eq!(serde_json::to_value(batch).unwrap(), example);
}

#[test]
fn a_batch_rejects_stale_evidence_misordered_steps_and_invalid_stage_rows() {
    for (index, field, bad) in [
        (5, "session", json!("another-session")),
        (7, "stamp", json!("stamp-1")),
        (8, "review", json!("review-1")),
        (9, "head", json!(HEAD)),
        (10, "run", json!(457)),
    ] {
        let mut example = sample();
        example["rows"][index]["step"][field] = bad;
        assert!(validate(example).is_err(), "step {index} accepted {field}");
    }
    let mut example = sample();
    let (earlier, later) = example["rows"].as_array_mut().unwrap().split_at_mut(4);
    std::mem::swap(&mut earlier[3]["step"], &mut later[0]["step"]);
    assert_eq!(
        validate(example).unwrap_err(),
        "step 3: step out of order or input does not match"
    );

    let mut example = sample();
    example["rows"][2]["stage"]["ended_at_ms"] = json!(0);
    assert!(validate(example).is_err());

    let mut example = sample();
    example["rows"][0]["step"]["result"] = json!({"status":"failed","reason":"claim held"});
    assert!(validate(example).is_err());
}

#[test]
fn claim_retry_validates_through_train_join() {
    let mut example = sample();
    add_failed_attempt(&mut example, 0, "claim held");
    validate(example.clone()).unwrap();

    example["rows"][1]["stage"]["attempt"] = json!(1);
    assert_eq!(
        validate(example).unwrap_err(),
        "invalid stage row for step 0 attempt 2"
    );
}

#[test]
fn fix_retry_resumes_checks_and_validates_through_train_join() {
    let mut example = sample();
    add_failed_attempt(&mut example, 5, "writer unavailable");
    validate(example.clone()).unwrap();

    example["rows"][7]["stage"]["attempt"] = json!(3);
    assert_eq!(
        validate(example).unwrap_err(),
        "invalid stage row for step 6 attempt 1"
    );
}

#[test]
fn failed_attempt_requires_a_nonblank_reason() {
    let mut example = sample();
    add_failed_attempt(&mut example, 0, " \t");
    assert_eq!(
        validate(example).unwrap_err(),
        "step 0: failed attempt requires a reason"
    );
}
