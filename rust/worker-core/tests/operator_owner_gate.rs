use pg_worker_core::operator_owner_gate::handle;
use serde_json::{json, Value};

fn item() -> Value {
    json!({"id":"Q", "owner":"operator", "operator":true, "kind":"question",
        "risk_tag":"money", "epoch":0, "choices":{"default":"yes","deadline":"2027-01-01"}})
}
#[test]
fn owner_gate_warn_refuse_and_rule() {
    let mut after = item();
    after["kind"] = json!("rule");
    assert_eq!(handle(json!({"after":after})).unwrap()["refused"], false);
    assert_eq!(
        handle(json!({"after":after,"mode":"refuse"})).unwrap()["refused"],
        true
    );
}
#[test]
fn owner_gate_requires_current_trusted_event() {
    let before = item();
    let mut after = before.clone();
    after["risk_tag"] = json!("taste");
    let event = json!({"seq":2,"type":"question_answered","role":"router","actor":"operator",
        "payload":{"source":"answer","risk_tag":"money","epoch":0}});
    let request = json!({"after":after,"before":before,"mode":"refuse",
        "operator_principal":"operator","events":[event]});
    assert_eq!(handle(request.clone()).unwrap()["refused"], false);
    let mut forged = request.clone();
    forged["events"][0]["actor"] = json!("router");
    assert_eq!(handle(forged).unwrap()["refused"], true);
    let mut timeout = request.clone();
    timeout["events"][0]["payload"]["source"] = json!("question_timeout");
    assert_eq!(handle(timeout).unwrap()["refused"], true);
    let mut old_epoch = request.clone();
    old_epoch["events"][0]["payload"]["epoch"] = json!(1);
    assert_eq!(handle(old_epoch).unwrap()["refused"], true);
    let mut stale = request;
    stale["events"]
        .as_array_mut()
        .unwrap()
        .push(json!({"seq":3,"type":"item_defined",
        "payload":{"fields":["risk_tag"]}}));
    assert_eq!(handle(stale).unwrap()["refused"], true);
}
#[test]
fn owner_gate_until_boundary_and_invalid_date() {
    let result = handle(json!({"today":"2027-01-01","items":[
        {"key":"future","until":"2027-01-02"},{"key":"today","until":"2027-01-01"},
        {"key":"past","until":"2026-01-01"}]}))
    .unwrap();
    assert_eq!(result["hidden"], json!(["future"]));
    let mut after = item();
    after["until"] = json!("2027-02-30");
    assert_eq!(
        handle(json!({"after":after,"mode":"refuse"})).unwrap()["refused"],
        true
    );
    assert!(handle(json!({"after":item(),"mode":"invalid"})).is_err());
}

#[test]
fn owner_gate_sqlite_integer_flag_cannot_be_cleared() {
    let mut before = item();
    before["owner"] = json!("routing");
    before["operator"] = json!(1);
    let mut after = before.clone();
    after["operator"] = json!(0);
    assert_eq!(
        handle(json!({"before":before,"after":after,"mode":"refuse"})).unwrap()["refused"],
        true
    );
}
