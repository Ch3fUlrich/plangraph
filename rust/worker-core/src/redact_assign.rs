//! Tight ALL_UPPERCASE=value env assignments are never source-exempt. Otherwise
//! preserve only f/b/r-prefixed strings containing both braces (including rf/fr
//! and triple quotes), direct dotted identifier calls/indexes balanced on the
//! same line, and ++/-- directly before a digit-free identifier ending at EOL
//! or `;`. Scan preserved expressions for nested credentials too. Plain quoted
//! literals are masked with quotes and expression suffixes kept. Colon separator
//! whitespace may cross newlines; equals separator whitespace may not. Bare
//! words, operators, containers and !/~ prefixes are not exempt. Independent
//! secret-pattern rules still apply.

use std::sync::OnceLock;

fn patterns() -> &'static [regex::Regex] {
    static RS: OnceLock<Vec<regex::Regex>> = OnceLock::new();
    RS.get_or_init(|| {
        [
            r#"^(?i:(fr|rf|br|rb|f|b|r))?("""(?:\\.|[^\\\r\n])*?"""|'''(?:\\.|[^\\\r\n])*?'''|"(?:\\.|[^"\\\r\n])*"|'(?:\\.|[^'\\\r\n])*')"#,
            r"^(?:\+\+|--)[A-Za-z_]+(?:;|$)",
            r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*[\[(]",
        ]
        .into_iter()
        .map(|pattern| regex::Regex::new(pattern).expect("assignment source pattern compiles"))
        .collect()
    })
}

fn source_end(value: &str) -> usize {
    let rs = patterns();
    if let Some(literal) = rs[0].captures(value) {
        let quoted = &literal[2];
        return if literal.get(1).is_some() && quoted.contains('{') && quoted.contains('}') {
            literal.get(0).unwrap().end()
        } else {
            0
        };
    }
    if let Some(prefix) = rs[1].find(value) {
        return prefix.end();
    }
    let Some(call) = rs[2].find(value) else {
        return 0;
    };
    let mut stack = Vec::new();
    let mut index = call.end() - 1;
    let bytes = value.as_bytes();
    while index < bytes.len() {
        let byte = bytes[index];
        if matches!(byte, b'"' | b'\'') {
            let Some(quoted) = rs[0].find(&value[index..]) else {
                return 0;
            };
            index += quoted.end();
            continue;
        }
        if matches!(byte, b'(' | b'[' | b'{') {
            stack.push(byte);
        } else if let Some(closer) = b")]}".iter().position(|b| *b == byte) {
            if stack.pop() != Some(b"([{"[closer]) {
                return 0;
            }
            if stack.is_empty() {
                return index + 1;
            }
        }
        index += 1;
    }
    0
}

fn value_end(value: &str, limit: usize) -> usize {
    let mut stack = Vec::new();
    let mut index = 0;
    let bytes = value.as_bytes();
    while index < bytes.len().min(limit) {
        let byte = bytes[index];
        if matches!(byte, b'"' | b'\'') {
            let Some(quoted) = patterns()[0].find(&value[index..]) else {
                return bytes.len().min(limit);
            };
            index += quoted.end();
            continue;
        }
        if matches!(byte, b'(' | b'[' | b'{') {
            stack.push(byte);
        } else if let Some(closer) = b")]}".iter().position(|b| *b == byte) {
            if stack.is_empty() {
                break;
            }
            if stack.pop() != Some(b"([{"[closer]) {
                return bytes.len().min(limit);
            }
        } else if byte == b',' && stack.is_empty() {
            break;
        }
        index += 1;
    }
    index.min(limit)
}

pub(super) fn redact(text: &str, pattern: &regex::Regex) -> String {
    let mut out = String::new();
    let (mut copied, mut cursor) = (0, 0);
    let mut source_ends = Vec::new();
    while let Some(caps) = pattern.captures_at(text, cursor) {
        let value = caps.get(4).unwrap();
        let start = value.start();
        let rhs = text[start..].split(['\r', '\n']).next().unwrap_or("");
        while source_ends.last().is_some_and(|end| start >= *end) {
            source_ends.pop();
        }
        let env = caps[1]
            .bytes()
            .all(|b| b.is_ascii_uppercase() || b.is_ascii_digit() || b == b'_')
            && &caps[3] == "=";
        let source = source_end(rhs);
        if source > 0 && !env {
            source_ends.push(start + source);
            cursor = start; // Exempt outer expressions must not hide nested credentials.
            continue;
        }
        let mut end = value.end();
        let mut replacement = super::REDACTED.to_string();
        if let Some(literal) = patterns()[0].captures(rhs).filter(|c| c.get(1).is_none()) {
            let quote = if rhs.starts_with("\"\"\"") || rhs.starts_with("'''") {
                &rhs[..3]
            } else {
                &rhs[..1]
            };
            end = start + literal.get(0).unwrap().end();
            replacement = format!("{quote}{}{quote}", super::REDACTED);
        } else if let Some(source_end) = source_ends.last() {
            end = start + value_end(rhs, source_end - start - 1);
        } else if rhs.starts_with(super::REDACTED) {
            let offset = super::REDACTED.len();
            end = start
                + rhs[offset..]
                    .find([')', '}', ']', '&'])
                    .map_or(rhs.len(), |n| offset + n);
        } else if source > 0 || patterns()[2].is_match(rhs) {
            end = start + rhs.len();
        }
        if end > start {
            out.push_str(&text[copied..start]);
            out.push_str(&replacement);
            copied = end;
        }
        cursor = start.max(end);
    }
    out.push_str(&text[copied..]);
    out
}
