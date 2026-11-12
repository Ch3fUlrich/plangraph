use pg_mcp::{handle, tools_list};
use serde_json::{json, Value};

#[test]
fn tools_equal_fixture() {
    let expected: Value = serde_json::from_str(include_str!(
        "../../../tests/fixtures/keeper_mcp_tools.json"
    ))
    .unwrap();
    assert_eq!(tools_list(), expected);
}

#[test]
fn replies_equal_python_bytes() {
    let records: Value = serde_json::from_str(include_str!(
        "../../../tests/fixtures/keeper_mcp_replies.json"
    ))
    .unwrap();
    for record in records.as_array().unwrap() {
        let actual = handle(&record["request"]).unwrap();
        assert_eq!(
            actual.as_bytes(),
            record["response"].as_str().unwrap().as_bytes()
        );
    }
}

#[test]
fn calls_are_explicitly_not_implemented() {
    for tool in tools_list().as_array().unwrap() {
        let reply = handle(&json!({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": tool["name"], "arguments": {}}}))
        .unwrap();
        let reply: Value = serde_json::from_str(&reply).unwrap();
        assert_eq!(reply["id"], 4);
        assert_eq!(reply["result"]["isError"], true);
        assert_eq!(
            reply["result"]["content"][0]["text"],
            "not_implemented: tools/call"
        );
    }
}

#[test]
fn notifications_are_silent() {
    for request in [
        json!({"method": "notifications/initialized"}),
        json!({"method": "tools/list"}),
        json!({"id": null, "method": "initialize"}),
        json!({"method": "tools/call", "params": {"name": "claim"}}),
    ] {
        assert_eq!(handle(&request), None);
    }
}
