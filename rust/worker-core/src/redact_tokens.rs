//! One token-body policy for ordinary and wrapped prefixed secrets.
//!
//! sk, gh*, github_pat, and xox bodies accept ASCII alphanumerics, `_`, and
//! `-`; AIza accepts the same alphabet, while AKIA accepts uppercase and digits.
//! Plain and wrapped paths share each family's minimum body length below.
//! LF/CRLF continuations consume the full contiguous body on each joined line;
//! after the floor, whole allowed-byte lines are consumed, including words.
//! Lines containing disallowed bytes survive. Joining stops at MAX_WRAPPED_BODY.
//! Below-floor bodies survive; AKIA retains its exact plain length and boundary.
pub(crate) const MAX_WRAPPED_BODY: usize = 4096;

pub(crate) struct TokenSpec {
    pub prefix: &'static str,
    pub body: &'static str,
    pub floor: usize,
    pub exact: bool,
}

impl TokenSpec {
    pub(crate) fn wrapped_limit(&self) -> usize {
        MAX_WRAPPED_BODY
    }
}

pub(crate) const SPECS: &[TokenSpec] = &[
    TokenSpec {
        prefix: "x?sk-",
        body: "[A-Za-z0-9_-]",
        floor: 1,
        exact: false,
    },
    TokenSpec {
        prefix: "gh[pousr]_",
        body: "[A-Za-z0-9_-]",
        floor: 20,
        exact: false,
    },
    TokenSpec {
        prefix: "github_pat_",
        body: "[A-Za-z0-9_-]",
        floor: 20,
        exact: false,
    },
    TokenSpec {
        prefix: "xox[abprs]-",
        body: "[A-Za-z0-9_-]",
        floor: 10,
        exact: false,
    },
    TokenSpec {
        prefix: "AKIA",
        body: "[0-9A-Z]",
        floor: 16,
        exact: true,
    },
    TokenSpec {
        prefix: "AIza",
        body: "[0-9A-Za-z_-]",
        floor: 30,
        exact: false,
    },
    TokenSpec {
        prefix: "[sr]k_(?:live|test)_",
        body: "[A-Za-z0-9]",
        floor: 16,
        exact: false,
    },
];

pub(crate) fn pattern() -> String {
    SPECS
        .iter()
        .map(|s| {
            let suffix = if s.exact { "}\\b" } else { ",}" };
            format!(r"\b{}{}{{{}{suffix}", s.prefix, s.body, s.floor)
        })
        .collect::<Vec<_>>()
        .join("|")
}

pub(crate) fn redact_url_userinfo(text: &str) -> String {
    static URL: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
    URL.get_or_init(|| {
        regex::Regex::new(r#"(\b[A-Za-z][A-Za-z0-9+.-]*://[^\s/@:"']+:)[^\s"']*(@)"#)
            .expect("userinfo pattern compiles")
    })
    .replace_all(text, "$1[REDACTED]$2")
    .into_owned()
}

pub(crate) fn is_allowlisted_sha(
    token: &str,
    allow_shas: &std::collections::BTreeSet<String>,
) -> bool {
    token.len() == 40 && allow_shas.contains(&token.to_ascii_lowercase())
}

pub(crate) fn redact_blob_plain(text: &str, blob_re: &regex::Regex) -> String {
    blob_re
        .replace_all(text, |m: &regex::Captures| {
            let t = m.get(0).unwrap().as_str();
            let has_digit = t.bytes().any(|b| b.is_ascii_digit());
            let has_alpha = t.bytes().any(|b| b.is_ascii_alphabetic());
            if has_digit && has_alpha {
                crate::redact::REDACTED.to_string()
            } else {
                t.to_string()
            }
        })
        .into_owned()
}

/// True for None/True/False/null or an already masked value, or for one call,
/// subscript or bracketed expression (with attribute/call chains) holding no
/// string literal. Spawner-read source keeps these readable; anything else after a
/// credential name is masked (src/plangraph/spawner/redact_tokens.py is_computed).
/// `value` must be the whole right-hand side, to the end of the line.
pub(crate) fn is_computed(value: &str) -> bool {
    let value = value.trim();
    if matches!(
        value,
        "None" | "True" | "False" | "null" | "[REDACTED]" | "\"[REDACTED]\"" | "'[REDACTED]'"
    ) {
        return true;
    }
    if value.is_empty() || value.contains('"') || value.contains('\'') {
        return false;
    }
    let chars: Vec<char> = value.chars().collect();
    let mut at = 0;
    if chars[0].is_ascii_alphabetic() || chars[0] == '_' {
        while at < chars.len()
            && (chars[at].is_ascii_alphanumeric() || matches!(chars[at], '_' | '.'))
        {
            at += 1;
        }
        while at < chars.len() && chars[at].is_whitespace() && !matches!(chars[at], '\r' | '\n') {
            at += 1;
        }
    }
    let closer = |c: char| match c {
        '(' => Some(')'),
        '[' => Some(']'),
        '{' => Some('}'),
        _ => None,
    };
    if at >= chars.len() || closer(chars[at]).is_none() {
        return false;
    }
    while at < chars.len() {
        let Some(first) = closer(chars[at]) else {
            return false;
        };
        let mut stack = vec![first];
        at += 1;
        while !stack.is_empty() && at < chars.len() {
            let c = chars[at];
            if let Some(end) = closer(c) {
                stack.push(end);
            } else if Some(&c) == stack.last() {
                stack.pop();
            } else if matches!(c, ')' | ']' | '}') {
                return false;
            }
            at += 1;
        }
        if !stack.is_empty() {
            return false;
        }
        if chars.get(at) == Some(&'.')
            && chars
                .get(at + 1)
                .is_some_and(|c| c.is_ascii_alphabetic() || *c == '_')
        {
            at += 1;
            while at < chars.len() && (chars[at].is_ascii_alphanumeric() || chars[at] == '_') {
                at += 1;
            }
        }
    }
    true
}

/// Mask the value of every assignment `rule` finds. In source mode a value that is a
/// computed expression, judged on the whole rest of its line, stays readable; any
/// other unquoted value is masked through the end of the line, so nothing after the
/// first closing delimiter can survive.
pub(crate) fn replace_assignments(rule: &regex::Regex, text: &str, source: bool) -> String {
    let mut out = String::with_capacity(text.len());
    let mut copied = 0;
    for m in rule.captures_iter(text) {
        let whole = m.get(0).unwrap();
        if whole.start() < copied {
            continue;
        }
        let value = m.get(4).unwrap();
        let line_end = text[value.start()..]
            .find(['\n', '\r'])
            .map_or(text.len(), |i| value.start() + i);
        let mut end = whole.end();
        if source {
            if is_computed(&text[value.start()..line_end]) {
                continue;
            }
            if !value.as_str().starts_with(['"', '\'']) {
                end = end.max(line_end);
            }
        }
        out.push_str(&text[copied..whole.start()]);
        out.push_str(&format!(
            "{}{}{}{}",
            &m[1],
            &m[2],
            &m[3],
            crate::redact::REDACTED
        ));
        copied = end;
    }
    out.push_str(&text[copied..]);
    out
}

pub(crate) fn redact_blob(
    token: &str,
    blob_re: &regex::Regex,
    hex_re: &regex::Regex,
    allow_shas: &std::collections::BTreeSet<String>,
) -> String {
    let has_digit = token.bytes().any(|b| b.is_ascii_digit());
    let has_alpha = token.bytes().any(|b| b.is_ascii_alphabetic());
    if !(has_digit && has_alpha) {
        return token.to_string();
    }
    if allow_shas.is_empty() {
        return crate::redact::REDACTED.to_string();
    }
    if is_allowlisted_sha(token, allow_shas) {
        return token.to_string();
    }
    let mut out = String::new();
    let mut copied = 0;
    let mut kept = false;
    for m in hex_re.find_iter(token) {
        if !is_allowlisted_sha(m.as_str(), allow_shas) {
            continue;
        }
        let flank = &token[copied..m.start()];
        if !path_flank_ok(token[..m.start()].chars().next_back(), flank)
            || !path_flank_ok(token[m.end()..].chars().next(), "")
        {
            return crate::redact::REDACTED.to_string();
        }
        out.push_str(&redact_blob_plain(flank, blob_re));
        out.push_str(m.as_str());
        copied = m.end();
        kept = true;
    }
    if !kept {
        return crate::redact::REDACTED.to_string();
    }
    let tail = &token[copied..];
    if hex_shaped(tail) {
        return crate::redact::REDACTED.to_string();
    }
    out.push_str(&redact_blob_plain(tail, blob_re));
    out
}

/// A kept sha may only sit in path context: the neighbour is the token edge or `/`,
/// and the text before it holds no hex-shaped fragment.
fn path_flank_ok(neighbour: Option<char>, flank: &str) -> bool {
    matches!(neighbour, None | Some('/')) && !hex_shaped(flank)
}

/// True when `text` holds a run of 8 or more hex digits.
fn hex_shaped(text: &str) -> bool {
    let mut run = 0;
    for b in text.bytes() {
        run = if b.is_ascii_hexdigit() { run + 1 } else { 0 };
        if run >= 8 {
            return true;
        }
    }
    false
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn computed_values_are_single_literal_free_expressions() {
        for yes in [
            "tuple(sorted((left, right)))",
            "(scope, name)",
            "[]",
            "{}",
            "vault[name]",
            "a.b(c).d(e)",
            "f (a)",
            "None",
            "\"[REDACTED]\"",
        ] {
            assert!(is_computed(yes), "{yes}");
        }
        for no in [
            "load(x) + literal",
            "get_secret() or x",
            "foo(bar",
            "foo)",
            "x",
            "123",
            "\"x\"",
            "os.environ[\"X\"]",
            "",
        ] {
            assert!(!is_computed(no), "{no}");
        }
    }

    #[test]
    fn wrapped_xox_underscore_is_redacted() {
        let prefix = ["xo", "xb-"].concat();
        let plain = format!("{prefix}ab_cd_ef_gh; retained");
        assert_eq!(crate::redact::redact(&plain), "[REDACTED]; retained");
        for newline in ["\n", "\r\n"] {
            let input = format!("{prefix}ab_{newline}cd_ef_gh; retained");
            assert_eq!(crate::redact::redact(&input), "[REDACTED]; retained");
        }
    }

    #[test]
    fn every_family_floor_and_allowed_body_match_wrapped_and_plain() {
        let families = [
            (0, "s", "k-"),
            (0, "xs", "k-"),
            (1, "gh", "p_"),
            (1, "gh", "o_"),
            (1, "gh", "u_"),
            (1, "gh", "s_"),
            (1, "gh", "r_"),
            (2, "github", "_pat_"),
            (3, "xo", "xa-"),
            (3, "xo", "xb-"),
            (3, "xo", "xp-"),
            (3, "xo", "xr-"),
            (3, "xo", "xs-"),
            (4, "AK", "IA"),
            (5, "AI", "za"),
            (6, "sk", "_live_"),
            (6, "sk", "_test_"),
            (6, "rk", "_live_"),
            (6, "rk", "_test_"),
        ];
        for (index, left, right) in families {
            let spec = &SPECS[index];
            let prefix = [left, right].concat();
            for len in [spec.floor - 1, spec.floor] {
                let alphabet = regex::Regex::new(&format!("^{}$", spec.body)).unwrap();
                let chars: Vec<char> = "A_-9"
                    .chars()
                    .filter(|c| alphabet.is_match(&c.to_string()))
                    .collect();
                let body: String = chars.iter().cycle().take(len).collect();
                for newline in ["\n", "\r\n"] {
                    let input = format!("{prefix}{newline}{body}; retained");
                    let expected = if len == spec.floor {
                        "[REDACTED]; retained"
                    } else {
                        &input
                    };
                    assert_eq!(crate::redact::redact(&input), expected, "family {index}");
                    assert_eq!(
                        crate::redact::redact_source(&input),
                        expected,
                        "family {index}"
                    );
                }
                let input = format!("{prefix}{body}; retained");
                let expected = if len == spec.floor {
                    "[REDACTED]; retained"
                } else {
                    &input
                };
                assert_eq!(crate::redact::redact(&input), expected, "family {index}");
                assert_eq!(
                    crate::redact::redact_source(&input),
                    expected,
                    "family {index}"
                );
            }
        }
    }
}
