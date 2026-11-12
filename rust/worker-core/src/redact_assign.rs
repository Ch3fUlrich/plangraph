//! Credential assignments mask every RHS except exactly four source forms:
//! f/b/r-prefixed strings containing both braces (including rf/fr and triple
//! quotes); an identifier (dotted allowed) directly followed by `(` or `[`;
//! `++`/`--`/`!`/`~` directly before an identifier; or a quoted literal followed
//! by a symbolic operator, attribute or index (horizontal space allowed before
//! the suffix). Bare/dotted words, operators between bare words (spaced or
//! tight), word operators and/or/if/else/in/is, and bare container starts are
//! not exempt. Independent secret-pattern rules still apply.

use std::sync::OnceLock;

fn patterns() -> &'static [regex::Regex] {
    static RS: OnceLock<Vec<regex::Regex>> = OnceLock::new();
    RS.get_or_init(|| {
        [
            r#"^(?i:(fr|rf|br|rb|f|b|r))?("""(?:\\.|[^\\\r\n])*?"""|'''(?:\\.|[^\\\r\n])*?'''|"(?:\\.|[^"\\\r\n])*"|'(?:\\.|[^'\\\r\n])*')"#,
            r"^(?:[+*/%&|^<>=!?]|\.[A-Za-z_]|\[|-(?:[^-]|$))",
            r"^(?:(?:\+\+|--|[!~])[A-Za-z_]|[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*[\[(])",
        ]
        .into_iter()
        .map(|pattern| regex::Regex::new(pattern).expect("assignment source pattern compiles"))
        .collect()
    })
}

fn is_source(value: &str) -> bool {
    if value.starts_with(super::REDACTED) {
        return false;
    }
    let rs = patterns();
    if let Some(literal) = rs[0].captures(value) {
        let quoted = &literal[2];
        return (literal.get(1).is_some() && quoted.contains('{') && quoted.contains('}'))
            || rs[1].is_match(value[literal.get(0).unwrap().end()..].trim_start());
    }
    rs[2].is_match(value)
}

pub(super) fn replacement(caps: &regex::Captures<'_>, text: &str) -> String {
    // A regex match may stop before a closing delimiter or a quoted literal's
    // operator suffix. Inspect the full RHS without crossing a physical line.
    let start = caps.get(4).unwrap().start();
    let rhs = text[start..].split(['\r', '\n']).next().unwrap_or("");
    if is_source(rhs) {
        caps[0].to_string()
    } else {
        format!("{}{}{}{}", &caps[1], &caps[2], &caps[3], super::REDACTED)
    }
}
