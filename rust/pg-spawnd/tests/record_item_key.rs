#![cfg(unix)]
#[path = "record_item_key_tests.rs"]
mod fixture;
use fixture::{payload, Fixture};
use serde_json::{json, Value};

#[test]
fn null_item_key_uses_completion_item() {
    for (item, expected) in [("x", json!("x")), ("bad/item", Value::Null)] {
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
        (json!("p:card"), None, json!("p:card")),
        (Value::Null, Some("p:env"), json!("p:env")),
        (json!("p:card"), Some("p:env"), json!("p:card")),
        (Value::Null, None, Value::Null),
        (json!("bad/item"), Some("p:env"), Value::Null),
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
        if item == "bad/item" {
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
    let mut f = Fixture::new(None);
    let r = payload(f.call("spawn", json!({"prompt":"p", "card":{"item_key":"plangraph:D-5047"}, "wait_seconds":5})));
    assert_eq!(r["item_key"], "plangraph:D-5047");
}
