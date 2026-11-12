//! The same synthetic credential table exercised by Python.

use super::{redact, redact_source};

#[derive(serde::Deserialize)]
#[serde(untagged)]
enum Text {
    Plain(String),
    Parts(Vec<String>),
}

impl Text {
    fn joined(&self) -> String {
        match self {
            Self::Plain(text) => text.clone(),
            Self::Parts(parts) => parts.concat(),
        }
    }
}

#[derive(serde::Deserialize)]
struct Row {
    input: Option<String>,
    #[serde(default)]
    input_parts: Vec<String>,
    expected: Option<Text>,
    source: Option<Text>,
    output: Option<Text>,
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
                (
                    row.output.as_ref().or(row.expected.as_ref()),
                    redact(&input),
                ),
                (row.source.as_ref(), redact_source(&input)),
            ] {
                if let Some(expected) = expected.map(Text::joined).filter(|s| *s != actual) {
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

#[test]
fn redact_wrapped_assignments_are_masked_whole() {
    for nl in ["\n", "\r\n"] {
        let secret = ["gh", "p_", &"A9".repeat(7), nl, &"A9".repeat(8)].concat();
        for (prefix, suffix) in [
            ("API_KEY=\"", "\""),
            ("export API_KEY=", ""),
            ("token: ", ""),
        ] {
            let input = format!("{prefix}{secret}{suffix}{nl}next retained");
            let source = format!("{prefix}[REDACTED]{suffix}{nl}next retained");
            // Plain mode removes value quotes; source mode preserves them.
            let plain = format!(
                "{}[REDACTED]{nl}next retained",
                prefix.trim_end_matches('"')
            );
            assert_eq!(redact(&input), plain);
            assert_eq!(redact(&plain), plain);
            assert_eq!(redact_source(&input), source);
            assert_eq!(redact_source(&source), source);
        }
    }
}
