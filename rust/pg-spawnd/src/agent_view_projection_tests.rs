use super::*;
#[test]
fn scalars_aliases_age_and_runtime() {
    let row = project(
        &json!({"name":true,"run_id":"worker","item_key":"PROJECT:ITEM","runtime":"invalid","started":"1970-01-01T00:00:10Z","model":[],"credential":"excluded"}),
        "local",
        None,
        Some(20.0),
    );
    assert_eq!(row["name"], "worker");
    assert_eq!(row["age_s"], 10.0);
    assert!(row["runtime"].is_null());
    assert!(row["model"].is_null());
    assert_eq!(row.as_object().unwrap().len(), 7);
    assert!(!row.to_string().contains("excluded"));
}
#[test]
fn malformed_hosts_and_rows_isolated() {
    let view = project_view(
        &json!({"hosts":[null,{"host":"local","readable":true,"agents":[null,{"name":"live","host":"spoofed"}]}]}),
    );
    assert_eq!(view["hosts"][0]["readable"], false);
    assert!(view["hosts"][1]["agents"][0]["name"].is_null());
    assert_eq!(view["hosts"][1]["agents"][1]["host"], "local");
}
#[test]
fn future_age_is_unknown() {
    assert!(project(&json!({"started":30}), "local", None, Some(20.0))["age_s"].is_null());
}
