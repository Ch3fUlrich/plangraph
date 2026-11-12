//! The same synthetic credential table exercised by Python.

use super::redact;

#[derive(serde::Deserialize)]
struct Row {
    input: String,
    expected: String,
}

#[test]
fn redact_shared_parity_table() {
    let rows: Vec<Row> = serde_json::from_str(include_str!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../tests/fixtures/redact_parity.json"
    )))
    .expect("shared parity table is valid JSON");
    let failures: Vec<_> = rows
        .iter()
        .filter_map(|row| {
            let actual = redact(&row.input);
            assert_eq!(redact(&actual), actual, "not idempotent: {:?}", row.input);
            (actual != row.expected).then(|| {
                format!(
                    "input {:?}: expected {:?}, got {:?}",
                    row.input, row.expected, actual
                )
            })
        })
        .collect();
    assert!(failures.is_empty(), "{}", failures.join("\n"));
}
