use super::*;
use serde_json::json;

const SENTINEL: &str = "SENTINEL-sandbox-value-7f3a";

/// The table of `test_plan_spawn_card_sandbox_contract` in tests/test_route_policy_review_choice.py.
fn cases() -> Vec<(Value, Option<&'static str>)> {
    vec![
        (json!({}), None),
        (json!({"sandbox": null}), None),
        (json!({"sandbox": "workspace-write"}), None),
        (
            json!({"sandbox": "danger-full-access"}),
            Some("danger-full-access"),
        ),
        (json!({"sandbox": SENTINEL}), Some("unsupported")),
        (json!({"sandbox": ""}), Some("unsupported")),
        (json!({"sandbox": "read-only"}), Some("unsupported")),
        (json!({"sandbox": 7}), Some("unsupported")),
        (json!({"sandbox": true}), Some("unsupported")),
        (json!({"sandbox": [SENTINEL]}), Some("unsupported")),
        (json!({"sandbox": {"k": SENTINEL}}), Some("unsupported")),
    ]
}

#[test]
fn card_sandbox_contract() {
    for (card, expected) in cases() {
        let result = refuse(&card);
        let Some(kind) = expected else {
            assert!(result.is_ok(), "{card}");
            continue;
        };
        let reason = result.unwrap_err();
        assert!(reason.starts_with(REFUSED), "{reason}");
        assert!(!reason.contains(SENTINEL), "{reason}");
        assert_eq!(
            reason.contains("danger-full-access"),
            kind == "danger-full-access"
        );
    }
}

#[test]
fn native_danger_full_access_contract_refuses_before_any_other_check() {
    let state = std::env::temp_dir().join("pg-card-sandbox-unused");
    for (mut card, expected) in cases() {
        let Some(kind) = expected else { continue };
        // No repo, no prompt: only the sandbox refusal can answer.
        card["run_kind"] = json!("native-codex");
        let args = json!({"card": card}).as_object().unwrap().clone();
        let message = crate::placement::native_spawn(&state, &args)
            .unwrap_err()
            .into_error_data()
            .message;
        assert!(message.starts_with(REFUSED), "{message}");
        assert!(!message.contains(SENTINEL), "{message}");
        assert_eq!(
            message.contains("danger-full-access"),
            kind == "danger-full-access"
        );
    }
}

#[test]
fn route_and_spawn_danger_full_access_refuse_first() {
    let state = std::env::temp_dir().join("pg-card-sandbox-unused");
    let args = json!({"card": {"sandbox": "danger-full-access"}, "model": "provider/model"})
        .as_object()
        .unwrap()
        .clone();
    let message = crate::placement::route(&state, &args)
        .unwrap_err()
        .into_error_data()
        .message;
    assert!(
        message.starts_with("sandbox_refused: danger-full-access"),
        "{message}"
    );
    assert!(check(&args).is_err());
}
