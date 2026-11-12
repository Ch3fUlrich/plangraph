#![cfg(unix)]
#[path = "record_item_key_tests.rs"]
mod fixture;
use fixture::{payload, Fixture};
use serde_json::{json, Value};

#[test]
fn null_item_key_uses_completion_item() {
    for (item, expected) in [
        ("plangraph:D-5047", json!("plangraph:D-5047")),
        ("bad/item", Value::Null),
        ("garbage", Value::Null),
        (":x", Value::Null),
    ] {
        let mut f = Fixture::new(None);
        let dir = f._state.path().join("runs/run-fallback");
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(
            dir.join("record.json"),
            json!({"run_id":"run-fallback", "item_key":null, "completion":{"item":item}})
                .to_string(),
        )
        .unwrap();
        let result = payload(f.call("ps", json!({})));
        assert_eq!(result["runs"][0]["item_key"], expected);
        let stored: Value =
            serde_json::from_str(&std::fs::read_to_string(dir.join("record.json")).unwrap())
                .unwrap();
        assert!(stored["item_key"].is_null());
    }
}

#[test]
fn record_item_key() {
    for (item, env, expected) in [
        (json!("plangraph:D-5047"), None, json!("plangraph:D-5047")),
        (
            Value::Null,
            Some("plangraph:D-5047"),
            json!("plangraph:D-5047"),
        ),
        (
            json!("plangraph:D-5047"),
            Some("plangraph:D-5048"),
            json!("plangraph:D-5047"),
        ),
        (Value::Null, None, Value::Null),
        (json!("bad/item"), Some("plangraph:D-5047"), Value::Null),
        (json!("garbage"), Some("plangraph:D-5047"), Value::Null),
        (json!(":x"), None, Value::Null),
        (json!(12), Some("plangraph:D-5047"), Value::Null),
        (Value::Null, Some("garbage"), Value::Null),
    ] {
        let mut f = Fixture::new(env);
        let route = payload(f.call("route", json!({"card":{"item":item,"estimate_tokens":100}})));
        assert!(route.get("error").is_none(), "{route}");
        let r = payload(f.call(
            "spawn",
            json!({"prompt":"p", "card":{"item":item}, "wait_seconds":5}),
        ));
        assert_eq!(r["status"], "completed");
        assert!(r.get("item_key").is_some());
        assert_eq!(r["item_key"], expected);
        let stored: Value = serde_json::from_str(
            &std::fs::read_to_string(
                f._state
                    .path()
                    .join("runs")
                    .join(r["run_id"].as_str().unwrap())
                    .join("record.json"),
            )
            .unwrap(),
        )
        .unwrap();
        assert_eq!(stored["item_key"], expected);
        if !item.is_null() && expected.is_null() {
            assert!(r["warnings"]
                .as_array()
                .unwrap()
                .iter()
                .any(|w| w.as_str().unwrap().contains("invalid item_key")));
        }
    }
}

#[test]
fn seat_card_item_key_alias() {
    for (alias, item, expected) in [
        (
            json!("plangraph:D-5047"),
            Value::Null,
            json!("plangraph:D-5047"),
        ),
        (
            json!("plangraph:D-5047"),
            json!("plangraph:D-5048"),
            json!("plangraph:D-5047"),
        ),
        (
            json!(""),
            json!("plangraph:D-5047"),
            json!("plangraph:D-5047"),
        ),
        (json!("garbage"), json!("plangraph:D-5047"), Value::Null),
        (json!(12), json!("plangraph:D-5047"), Value::Null),
    ] {
        let mut f = Fixture::new(Some("plangraph:D-5048"));
        let r = payload(f.call(
            "spawn",
            json!({"prompt":"p", "card":{"item_key":alias,"item":item}, "wait_seconds":5}),
        ));
        assert_eq!(r["status"], "completed");
        assert_eq!(r["item_key"], expected);
    }
}

#[test]
fn record_item_key_ignores_prompt_mentions() {
    let mut f = Fixture::new(None);
    let r = payload(f.call(
        "spawn",
        json!({"prompt":"Review plangraph:D-5047 now", "card":{"estimate_tokens":100}, "role":"review", "wait_seconds":5}),
    ));
    assert_eq!(r["status"], "completed");
    assert_eq!(r["item_key"], Value::Null);
}

#[test]
fn record_item_key_missing_event_is_persisted() {
    for dry in [false, true] {
        let mut f = Fixture::new(None);
        let r = payload(f.call(
            "spawn",
            json!({"prompt":"p", "card":{"estimate_tokens":100}, "role":"review", "dry_run":dry, "wait_seconds":5}),
        ));
        assert_eq!(r["status"], if dry { "dry_run" } else { "completed" });
        assert_eq!(r["item_key"], Value::Null);
        let expected = json!({"event":"seat_item_missing","level":"warning"});
        assert!(r["events"].as_array().unwrap().contains(&expected), "{r}");
        let stored: Value = serde_json::from_str(
            &std::fs::read_to_string(
                f._state
                    .path()
                    .join("runs")
                    .join(r["run_id"].as_str().unwrap())
                    .join("record.json"),
            )
            .unwrap(),
        )
        .unwrap();
        assert!(stored["events"].as_array().unwrap().contains(&expected));
    }
}
