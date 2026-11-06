#![cfg(unix)]
mod common;
#[path = "common/placement.rs"]
mod placement;
use placement::{payload, Fixture};
use serde_json::{json, Value};

#[test]
fn record_item_key() {
    // Fixtures inherit this environment only at daemon creation; no parallel mutation.
    for (item, env, expected) in [
        (json!("p:card"), None, json!("p:card")),
        (Value::Null, Some("p:env"), json!("p:env")),
        (json!("p:card"), Some("p:env"), json!("p:card")),
        (Value::Null, None, Value::Null),
        (json!("bad/item"), Some("p:env"), Value::Null),
    ] {
        match env {
            Some(s) => std::env::set_var("PLANGRAPH_SPAWN_ITEM", s),
            None => std::env::remove_var("PLANGRAPH_SPAWN_ITEM"),
        }
        let mut f = Fixture::new("exit 0", Value::Null, &[]);
        assert!(f.state.path().exists());
        assert!(payload(f.spawn(5.0)).get("item_key").is_some());
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
    std::env::remove_var("PLANGRAPH_SPAWN_ITEM");
}
