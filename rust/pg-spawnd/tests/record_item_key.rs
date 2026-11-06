#![cfg(unix)]
#[path = "record_item_key_tests.rs"]
mod fixture;
use fixture::{payload, Fixture};
use serde_json::{json, Value};

#[test]
fn record_item_key() {
    for (item, env, expected) in [
        (json!("p:card"), None, json!("p:card")),
        (Value::Null, Some("p:env"), json!("p:env")),
        (json!("p:card"), Some("p:env"), json!("p:card")),
        (Value::Null, None, Value::Null),
        (json!("bad/item"), Some("p:env"), Value::Null),
    ] {
        let mut f = Fixture::new(env);
        let r = payload(f.call(
            "spawn",
            json!({"prompt":"p", "card":{"item":item}, "wait_seconds":5}),
        ));
        assert_eq!(r["status"], "completed");
        assert!(r.get("item_key").is_some());
        assert_eq!(r["item_key"], expected);
        if item == "bad/item" {
            assert!(r["warnings"]
                .as_array()
                .unwrap()
                .iter()
                .any(|w| w.as_str().unwrap().contains("invalid item_key")));
        }
    }
}
