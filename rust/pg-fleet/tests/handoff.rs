use serde_json::Value;

#[test]
fn python_card_parity() {
    let cases: Vec<Value> = serde_json::from_str(include_str!("fixtures/handoff.json")).unwrap();
    for c in cases {
        assert_eq!(
            serde_json::json!(pg_fleet::handoff::check_text(c["text"].as_str().unwrap())),
            c["reasons"],
            "{}",
            c["name"]
        );
    }
}
