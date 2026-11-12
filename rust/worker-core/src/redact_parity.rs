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
            // D-5279 R3: Rust drops value quotes; Python's existing rule keeps them.
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

#[test]
fn redact_prefixed_line_before_my_secret_retains_main_assignment_scope() {
    let value = ["hunter", "2value"].concat();
    let standalone = format!("MY_SECRET: {value}");
    // Main's Rust assignment regex matches MY_SECRET, unlike Python's regex.
    assert_eq!(redact(&standalone), "MY_SECRET: [REDACTED]");
    for nl in ["\n", "\r\n"] {
        let text = format!(
            "x {}{}{nl}{standalone}",
            ["gh", "p_"].concat(),
            "A9".repeat(7)
        );
        assert_eq!(redact(&text), "x [REDACTED]: [REDACTED]");
    }
}

#[test]
fn redact_plain_order_never_reveals_pieces_masked_by_old_order() {
    fn old_order(text: &str) -> String {
        // Main's assignment-then-wrapped order; these inputs need no PEM/Bearer/URL rules.
        let rs = super::patterns();
        let out = rs[3..5].iter().fold(text.to_owned(), |out, rule| {
            rule.replace_all(&out, "$1$2$3[REDACTED]").into_owned()
        });
        let out = crate::redact_wrapped::redact_wrapped(&out);
        rs[5..7].iter().fold(out, |out, rule| {
            rule.replace_all(&out, "[REDACTED]").into_owned()
        })
    }

    let first = "A9".repeat(7);
    let rest = "B8".repeat(8);
    let value = ["hunter", "2value"].concat();
    let short = ["gh", "p_", &first].concat();
    let full = format!("{short}{}", "C7".repeat(3));
    let mut inputs = Vec::new();
    for nl in ["\n", "\r\n"] {
        inputs.extend([
            format!("x {short}{nl}API_KEY={value}"),
            format!("x {short}{nl}PASSWORD={value}"),
            format!("x {short}{nl}MY_SECRET: {value}"),
            format!("API_KEY={short}{nl}{rest}{nl}next"),
            format!("API_KEY=\"{short}{nl}{rest}\"{nl}next retained"),
            format!("API_KEY='{short}{nl}{rest}'{nl}next retained"),
            format!("API_KEY={short}{nl}PASSWORD={value}"),
            format!("API_KEY={short}{nl}PASSWORD=[REDACTED]"),
            format!("API_KEY=abc{nl}next line of prose"),
            format!("PASSWORD={rest}{nl}next line of prose"),
            format!("{full}{nl}API_KEY={value}"),
            format!("{full}{nl}API_KEY=[REDACTED]"),
            format!("API_KEY={short}{nl}next retained"),
            format!("API_KEY=\"{short}\"{nl}next retained"),
            format!("API_KEY=abc {short}{nl}next retained"),
        ]);
    }
    assert!(inputs.len() >= 12);
    let mut guarded = [false; 4];
    for text in inputs {
        let old = old_order(&text);
        let new = redact(&text);
        for (i, piece) in [&first, &rest, &full, &value].into_iter().enumerate() {
            if text.contains(piece) && !old.contains(piece) {
                guarded[i] = true;
                assert!(!new.contains(piece), "{text:?}: old {old:?}, new {new:?}");
            }
        }
    }
    assert!(guarded.into_iter().all(|tested| tested));
}
