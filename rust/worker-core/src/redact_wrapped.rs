//! Join incomplete prefixed tokens, consuming full bodies before testing floors.
use std::sync::OnceLock;

pub(crate) fn redact_wrapped(text: &str) -> String {
    static PREFIX: OnceLock<regex::Regex> = OnceLock::new();
    let re = PREFIX.get_or_init(prefix_regex);
    static BODIES: OnceLock<Vec<regex::Regex>> = OnceLock::new();
    let bodies = BODIES.get_or_init(|| {
        super::redact_tokens::SPECS
            .iter()
            .map(|s| regex::Regex::new(&format!("^{}$", s.body)).unwrap())
            .collect()
    });
    let mut out = String::new();
    let mut copied = 0;
    for captures in re.captures_iter(text) {
        let m = captures.get(0).unwrap();
        if m.start() < copied {
            continue;
        }
        let index = (1..captures.len())
            .find(|&i| captures.get(i).is_some())
            .unwrap()
            - 1;
        let floor = super::redact_tokens::SPECS[index].floor;
        let allowed =
            |b: u8| b.is_ascii() && bodies[index].is_match(std::str::from_utf8(&[b]).unwrap());
        let bytes = text.as_bytes();
        let mut end = m.end();
        let mut count = 0;
        let mut wrapped = false;
        loop {
            while end < bytes.len() && allowed(bytes[end]) {
                count += 1;
                end += 1;
            }
            if count >= floor {
                break;
            }
            let next = if bytes.get(end..end + 2) == Some(b"\r\n") {
                end + 2
            } else if bytes.get(end) == Some(&b'\n') {
                end + 1
            } else {
                break;
            };
            if !bytes.get(next).is_some_and(|b| allowed(*b)) {
                break;
            }
            end = next;
            wrapped = true;
        }
        // Deliberately preserve joined bodies below the family floor: short
        // prefix-like prose is not classified as a secret by either path.
        if wrapped && count >= floor {
            out.push_str(&text[copied..m.start()]);
            out.push_str(crate::redact::REDACTED);
            copied = end;
        }
    }
    out.push_str(&text[copied..]);
    out
}

fn prefix_regex() -> regex::Regex {
    let prefixes = super::redact_tokens::SPECS
        .iter()
        .map(|s| format!("({})", s.prefix))
        .collect::<Vec<_>>()
        .join("|");
    regex::Regex::new(&format!(r"\b(?:{prefixes})")).unwrap()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn prefix_capture_indices_identify_specs() {
        assert_eq!(
            prefix_regex().captures_len(),
            crate::redact_tokens::SPECS.len() + 1
        );
    }
}
