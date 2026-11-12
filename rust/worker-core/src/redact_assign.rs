//! Source-expression exclusions for the legacy credential-assignment rules.

use std::sync::OnceLock;

fn patterns() -> &'static [regex::Regex] {
    static RS: OnceLock<Vec<regex::Regex>> = OnceLock::new();
    RS.get_or_init(|| {
        [
            r#"^(?i:([fbr]{1,2}))?("(?:\\.|[^"\\\r\n])*"|'(?:\\.|[^'\\\r\n])*')"#,
            r"^(?:[+*/%&|^<>=!?]|\.[A-Za-z_]|\[|-(?:[^-]|$)|\b(?:and|or|if)\b)",
            r"^(?:[\[({]|(?:\+\+|--|[!~])[^\S\r\n]*[A-Za-z_]|[A-Za-z_][A-Za-z0-9_.]*[^\S\r\n]*[\[(])",
            r"[^\S\r\n](?:[+\-*/%&|^<>=!]+|and|or|if|else|in|is)[^\S\r\n]",
            r"^[a-z_][a-z0-9_]*$",
            r"^[a-z_]+(?:\.[a-z_]+)*$",
            r"^[a-z_]+[+*/%&|^<>=]+[a-z_]+$",
        ]
        .into_iter()
        .map(|pattern| regex::Regex::new(pattern).expect("assignment source pattern compiles"))
        .collect()
    })
}

fn is_source(name: &str, separator: &str, value: &str) -> bool {
    if value.starts_with(super::REDACTED) {
        return false;
    }
    let rs = patterns();
    if let Some(literal) = rs[0].captures(value) {
        let quoted = &literal[2];
        return (literal.get(1).is_some() && quoted.contains('{') && quoted.contains('}'))
            || rs[1].is_match(value[literal.get(0).unwrap().end()..].trim_start());
    }
    if rs[2].is_match(value) || rs[3].is_match(value) {
        return true;
    }
    let reference = value
        .trim()
        .strip_suffix(';')
        .unwrap_or(value.trim())
        .trim_end();
    separator.contains('=')
        && rs[4].is_match(name)
        && (rs[5].is_match(reference) || rs[6].is_match(reference))
}

pub(super) fn replacement(caps: &regex::Captures<'_>, text: &str) -> String {
    // A regex match may stop before a closing delimiter or a quoted literal's
    // operator suffix. Inspect the full RHS without crossing a physical line.
    let start = caps.get(4).unwrap().start();
    let rhs = text[start..].split(['\r', '\n']).next().unwrap_or("");
    if is_source(&caps[1], &caps[3], rhs) {
        caps[0].to_string()
    } else {
        format!("{}{}{}{}", &caps[1], &caps[2], &caps[3], super::REDACTED)
    }
}
