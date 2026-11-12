//! The same synthetic credential table exercised by Python.

use super::{redact, redact_source};

#[derive(serde::Deserialize)]
struct Row {
    input: Option<String>,
    #[serde(default)]
    input_parts: Vec<String>,
    expected: Option<String>,
    source: Option<String>,
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
            let input = row
                .input
                .clone()
                .unwrap_or_else(|| row.input_parts.concat());
            for (expected, actual) in [
                (row.expected.as_ref(), redact(&input)),
                (row.source.as_ref(), redact_source(&input)),
            ] {
                if expected.is_some_and(|expected| *expected != actual) {
                    return Some(format!(
                        "input {input:?}: expected {expected:?}, got {actual:?}"
                    ));
                }
            }
            None
        })
        .collect();
    assert!(failures.is_empty(), "{}", failures.join("\n"));
}

#[test]
fn redact_source_keeps_private_key_and_wrapped_rules() {
    let pem = ["-----BEGIN ", "TEST PRIVATE KEY-----\nfake\n"].concat();
    assert_eq!(redact_source(&pem), "[REDACTED]");
    let wrapped = ["sk", "-synthetic\nfixture\nkept prose"].concat();
    assert_eq!(redact_source(&wrapped), "[REDACTED]\nkept prose");
}
