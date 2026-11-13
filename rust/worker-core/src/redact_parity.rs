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
    // D-5279 R3: Python retains value quotes and quotes NEW JSON scalars;
    // Rust drops quoted values' quotes and keeps its unquoted stop characters.
    rust_expected: Option<Text>,
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
                    row.rust_expected
                        .as_ref()
                        .or(row.output.as_ref())
                        .or(row.expected.as_ref()),
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
fn redact_my_secret_assignment_shapes_are_masked() {
    let value = ["hunter", "2value"].concat();
    for sep in [": ", "="] {
        let standalone = format!("MY_SECRET{sep}{value}");
        assert_eq!(redact(&standalone), format!("MY_SECRET{sep}[REDACTED]"));
        for nl in ["\n", "\r\n"] {
            let text = format!(
                "x {}{}{nl}{standalone}",
                ["gh", "p_"].concat(),
                "A9".repeat(7)
            );
            assert_eq!(redact(&text), format!("x [REDACTED]{sep}[REDACTED]"));
        }
    }
}

#[test]
fn redact_plain_order_never_reveals_pieces_masked_by_old_order() {
    let first = "A9".repeat(7);
    let rest = "B8".repeat(8);
    let value = ["hunter", "2value"].concat();
    let tiny_body = format!("{}D", "D6".repeat(4));
    let extra = "C7".repeat(3);
    let short = ["gh", "p_", &first].concat();
    let full = format!("{short}{extra}");
    let tiny = ["gh", "p_", &tiny_body].concat();
    let full_body = format!("{first}{extra}");
    let pieces = [&first, &full_body, &extra, &rest, &tiny_body, &value];
    let mut inputs = Vec::new();
    let mut defects = Vec::new();
    for nl in ["\n", "\r\n"] {
        for line1 in [
            short.clone(),
            full.clone(),
            format!("API_KEY={short}"),
            format!("API_KEY=\"{short}"),
            "ordinary".to_owned(),
        ] {
            for line2 in [
                rest.clone(),
                format!("PASSWORD={tiny}"),
                format!("PASSWORD=\"{tiny}\""),
                format!("PASSWORD={value}"),
                "next line of prose".to_owned(),
            ] {
                for line3 in [
                    String::new(),
                    format!("{nl}next retained"),
                    format!("{nl}{rest}"),
                ] {
                    inputs.push(format!("{line1}{nl}{line2}{line3}"));
                }
            }
        }
        defects.extend([
            format!("API_KEY={short}{nl}{rest}{nl}next"),
            format!("API_KEY=\"{short}{nl}{rest}\"{nl}next retained"),
        ]);
    }
    assert!(inputs.len() >= 40);
    let mut guarded = [false; 6];
    for text in inputs.iter().chain(&defects) {
        let old = main_order(text);
        let new = redact(text);
        for (i, piece) in pieces.into_iter().enumerate() {
            if text.contains(piece) && !old.contains(piece) {
                guarded[i] = true;
                assert!(!new.contains(piece), "{text:?}: old {old:?}, new {new:?}");
            }
        }
        if defects.contains(text) {
            assert!(!new.contains(&rest), "{text:?}: {new:?}");
        }
    }
    assert!(guarded.into_iter().all(|tested| tested));
}

fn main_order(text: &str) -> String {
    // Main's primitives/order, without P; inputs need no PEM/Bearer/blob rules.
    let rs = super::patterns();
    let out = rs[3..5].iter().fold(text.to_owned(), |out, rule| {
        rule.replace_all(&out, "$1$2$3[REDACTED]").into_owned()
    });
    let out = crate::redact_wrapped::redact_wrapped(&out);
    let out = crate::redact_tokens::redact_url_userinfo(&out);
    rs[5..8].iter().fold(out, |out, rule| {
        rule.replace_all(&out, "[REDACTED]").into_owned()
    })
}

#[test]
fn redact_shield_markers_skip_prepass_and_equal_main() {
    for marker in ["\u{e000}", "\u{e001}", "\u{e000}0\u{e001}"] {
        for nl in ["\n", "\r\n"] {
            let text = format!(
                "{marker} API_KEY={}{}{nl}{}{nl}next",
                ["gh", "p_"].concat(),
                "A9".repeat(7),
                "B8".repeat(8)
            );
            assert_eq!(
                crate::redact_wrapped::join_shielded_assignments(&text, &super::patterns()[3..5]),
                text
            );
            assert_eq!(redact(&text), main_order(&text));
        }
    }
}

#[test]
fn redact_shielded_join_preserves_all_names_separators_and_values() {
    let join = |text: &str| {
        crate::redact_wrapped::join_shielded_assignments(text, &super::patterns()[3..5])
    };
    let text = (0..12)
        .map(|i| format!("\"API_KEY\" : value{i}"))
        .collect::<Vec<_>>()
        .join("\n");
    assert_eq!(join(&text), text);
    // Exercise both assignment regexes; values are untouched in P, including 4b's closers.
    for value in [
        ")abc",
        "]abc",
        "}abc",
        "[REDACTED]=[REDACTED]",
        "\"a\";retained",
    ] {
        let text = format!("API_KEY=\u{a0}{value}\nPASSWORD=plain");
        assert_eq!(join(&text), text);
    }
    for nl in ["\n", "\r\n"] {
        let text = format!(
            "API_KEY={}{}{nl}{}{nl}next retained",
            ["gh", "p_"].concat(),
            "A9".repeat(7),
            "B8".repeat(8)
        );
        assert_eq!(join(&text), format!("API_KEY=[REDACTED]{nl}next retained"));
    }
}

#[test]
fn redact_earlier_short_token_cannot_expose_below_floor_assignment() {
    let short = ["gh", "p_", &"A9".repeat(7)].concat();
    let tiny_body = format!("{}D", "D6".repeat(4));
    let tiny = ["gh", "p_", &tiny_body].concat();
    for nl in ["\n", "\r\n"] {
        for quote in ["", "\"", "'"] {
            for (lead, name, ending) in [
                ("x ", "API_KEY", String::new()),
                ("x ", "API_KEY", format!("{nl}end")),
                ("", "PASSWORD", String::new()),
            ] {
                let text = format!("{lead}{short}{nl}{name}={quote}{tiny}{quote}{ending}");
                let expected = format!("{lead}[REDACTED]=[REDACTED]{ending}");
                // Rust's main assignment rule drops value quotes; Python retains them.
                assert_eq!(redact(&text), expected);
                assert_eq!(redact(&text), main_order(&text));
                assert!(!redact(&text).contains(&tiny_body));
            }
        }
    }
}
