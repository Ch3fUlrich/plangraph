use std::{fs, process::Command};

fn snapshot() -> serde_json::Value {
    serde_json::json!({"pace":1,"budget":2,"ci_failed":0,"ci_total":0,
        "infra_failed":0,"review_age_hours":0,"review_rounds":0,
        "critical_idle_hours":0,"coding_vm_runs":0,"dell_idle":false,
        "host_drift":false,"closes_recent":0,"closes_previous":0})
}

fn events(s: &serde_json::Value) -> Vec<serde_json::Value> {
    let dir = tempfile::tempdir().unwrap();
    let input = dir.path().join("input.json");
    fs::write(&input, s.to_string()).unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_flow-watchdog"))
        .env_remove("PLANGRAPH_WATCHDOG_DELIVERY")
        .env_remove("PLANGRAPH_FLOW_PUSH")
        .arg(input)
        .arg(dir.path())
        .output()
        .unwrap();
    assert!(output.status.success(), "{output:?}");
    fs::read(dir.path().join("queue.json"))
        .map(|v| serde_json::from_slice(&v).unwrap())
        .unwrap_or_default()
}

macro_rules! check {
    ($test:ident, $field:literal, $rule:literal, $threshold:expr, $gate:literal) => {
        #[test]
        fn $test() {
            let mut s = snapshot();
            let mut row = serde_json::json!({"item":"item-1","owner":"owner-1",
                "age_minutes":$threshold, $gate:true});
            s[$field] = serde_json::json!([row.clone()]);
            assert!(events(&s).is_empty());
            row["age_minutes"] = serde_json::json!($threshold + 1);
            row[$gate] = false.into();
            s[$field] = serde_json::json!([row.clone()]);
            assert!(events(&s).is_empty());
            row[$gate] = true.into();
            s[$field] = serde_json::json!([row]);
            let found = events(&s);
            assert_eq!(found.len(), 1);
            let message = found[0]["message"].as_str().unwrap();
            assert_eq!(message.lines().count(), 1);
            for expected in [$rule, "item-1", "owner-1", "age_minutes="] {
                assert!(message.contains(expected), "{message}");
            }
        }
    };
}
check!(
    lapsed_lease,
    "lapsed_leases",
    "lapsed-lease",
    30,
    "open_lane"
);
check!(
    ordered_unclaimed,
    "ordered_items",
    "ordered-unclaimed",
    120,
    "unclaimed"
);
check!(
    owner_without_session,
    "ready_owners",
    "owner-no-session",
    60,
    "no_live_session"
);
check!(
    idle_capacity,
    "idle_capacity",
    "idle-capacity",
    30,
    "capacity_available"
);
check!(
    unanswered_request,
    "go_requests",
    "unanswered-go-req",
    60,
    "unanswered"
);
check!(
    merged_review,
    "merged_reviews",
    "merged-review-open",
    360,
    "in_review"
);
