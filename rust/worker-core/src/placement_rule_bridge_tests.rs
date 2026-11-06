use super::*;
use crate::placement_rule_store::RuleScope;

const KEY: &str = "PG_RULE_HOST_WORKER_CAP_COUNT";

fn report() -> Value {
    json!({"op":"report", "snapshot":{"revision":0,"fleet":{},"hosts":{}},
        "host_class":"", "host_id":"test", "process":{}, "env_file":{}})
}

fn edit(scope: Value, key: &str, value: Value) -> Value {
    json!({"op":"validate", "edit":{"scope":scope,"key":key,"value":value,
        "expected_revision":0,"rollback_of":null}})
}

#[test]
fn schema_specs_match_single_toml() {
    assert_eq!(
        handle(json!({"op":"specs"})).unwrap()["specs"],
        json!(schema().unwrap())
    );
    assert!(include_str!("../../../setup/placement-rules.schema.toml").contains("HostWorkerCap"));
}

#[test]
fn exact_scope_wire_roundtrip() {
    for scope in [json!("Fleet"), json!({"Host":"test"})] {
        let parsed: RuleScope = serde_json::from_value(scope.clone()).unwrap();
        assert_eq!(serde_json::to_value(parsed).unwrap(), scope);
        assert_eq!(handle(edit(scope, KEY, json!("4"))).unwrap(), json!({}));
    }
}

#[test]
fn unknown_op_fields_key_and_scope_refused() {
    for request in [
        json!({"op":"edit"}),
        json!({"op":"specs","actor":"admin"}),
        edit(json!("fleet"), KEY, json!("4")),
        edit(json!("Fleet"), "OTHER", json!("4")),
        edit(json!("Fleet"), "PG_RULE_UNKNOWN_COUNT", Value::Null),
    ] {
        assert_eq!(handle(request), Err(StoreError::Invalid));
    }
    let mut request = edit(json!("Fleet"), KEY, json!("4"));
    request["edit"]["extra"] = json!(true);
    assert_eq!(handle(request), Err(StoreError::Invalid));
    let mut request = report();
    request["snapshot"]["extra"] = json!(true);
    assert_eq!(handle(request), Err(StoreError::Invalid));
}

#[test]
fn allowlist_and_invalid_value_use_existing_validator() {
    for key in [KEY, "OTHER", "PG_RULE_UNKNOWN_COUNT"] {
        for value in [None, Some("4"), Some("-1"), Some("1025"), Some("1.5")] {
            assert_eq!(
                handle(edit(json!("Fleet"), key, json!(value))).map(|_| ()),
                validate_override(&RuleScope::Fleet, key, value)
            );
        }
    }
    let mut request = report();
    request["snapshot"]["hosts"]["unselected"] = json!({"OTHER":"4"});
    assert_eq!(handle(request), Err(StoreError::Invalid));
}

#[test]
fn report_precedence_all_five_levels() {
    let mut request = report();
    request["process"][KEY] = json!("1");
    request["env_file"][KEY] = json!("2");
    request["snapshot"]["hosts"]["test"] = json!({KEY:"3"});
    request["snapshot"]["fleet"][KEY] = json!("4");
    for (value, source) in [
        (1, "process"),
        (2, "env_file"),
        (3, "keeper_host"),
        (4, "keeper_fleet"),
        (6, "default"),
    ] {
        let result = handle(request.clone()).unwrap();
        assert_eq!(result["report"]["values"][KEY], value);
        assert_eq!(result["report"]["sources"][KEY], source);
        match source {
            "process" | "env_file" => request[source] = json!({}),
            "keeper_host" => request["snapshot"]["hosts"] = json!({}),
            _ => request["snapshot"]["fleet"] = json!({}),
        }
    }
}

#[test]
fn invalid_higher_layer_falls_back_with_diagnostic() {
    let mut request = report();
    request["process"][KEY] = json!("invalid");
    request["env_file"][KEY] = json!("2");
    let result = handle(request).unwrap();
    assert_eq!(result["report"]["values"][KEY], 2);
    assert_eq!(
        result["report"]["diagnostics"],
        json!([format!("{KEY} process invalid_unsigned")])
    );
}

#[test]
fn seeds_floor_6144_alarm_3000_cap_6_dispatch_4_prox_5() {
    for (class, cap) in [("", 6), ("dispatch_vm", 4), ("prox", 5)] {
        let mut request = report();
        request["host_class"] = json!(class);
        let result = handle(request).unwrap();
        assert_eq!(result["report"]["values"][KEY], cap);
        assert_eq!(
            result["report"]["values"]["PG_RULE_HOST_MEMORY_FLOOR_MIB"],
            6144
        );
        assert_eq!(result["specs"][0]["alarm"], 3000);
    }
}

#[test]
fn no_request_environment_or_actor_authority() {
    for field in ["actor", "environment", "credentials"] {
        let mut request = report();
        request[field] = json!("admin");
        assert_eq!(handle(request), Err(StoreError::Invalid));
    }
    assert_eq!(handle(report()).unwrap()["report"]["values"][KEY], 6);
}

#[test]
fn integer_and_host_bounds() {
    for host in [" ".into(), "a\n".into(), "é".repeat(33)] {
        let mut request = report();
        request["host_id"] = json!(host);
        assert_eq!(handle(request), Err(StoreError::Invalid));
        assert_eq!(
            handle(edit(json!({"Host":host}), KEY, json!("4"))),
            Err(StoreError::Invalid)
        );
    }
    for revision in [json!(-1), json!(1.5), json!(i64::MAX as u64 + 1)] {
        let mut request = report();
        request["snapshot"]["revision"] = revision.clone();
        assert_eq!(handle(request), Err(StoreError::Invalid));
        for field in ["expected_revision", "rollback_of"] {
            let mut request = edit(json!("Fleet"), KEY, json!("4"));
            request["edit"][field] = revision.clone();
            assert_eq!(handle(request), Err(StoreError::Invalid));
        }
    }
    let mut request = report();
    request["host_id"] = json!("a".repeat(64));
    request["snapshot"]["revision"] = json!(i64::MAX);
    assert!(handle(request).is_ok());
}
