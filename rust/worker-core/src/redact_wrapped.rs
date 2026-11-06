//! Join only incomplete prefixed tokens, stopping as soon as their floor is met.
use std::sync::OnceLock;

pub(crate) fn redact_wrapped(text: &str) -> String {
    static PREFIX: OnceLock<regex::Regex> = OnceLock::new();
    let re = PREFIX.get_or_init(|| {
        regex::Regex::new(r"\b(?:x?sk-|gh[pousr]_|github_pat_|xox[abprs]-|AKIA|AIza)").unwrap()
    });
    let mut out = String::new();
    let mut copied = 0;
    for m in re.find_iter(text) {
        if m.start() < copied {
            continue;
        }
        let prefix = m.as_str();
        let floor = if prefix.starts_with("gh") {
            20
        } else if prefix.starts_with("xox") {
            10
        } else if prefix == "AKIA" {
            16
        } else if prefix == "AIza" {
            30
        } else {
            20
        };
        let allowed = |b: u8| {
            b.is_ascii_alphanumeric()
                || (prefix != "AKIA" && !prefix.starts_with("ghp") && (b == b'_' || b == b'-'))
        };
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
        if wrapped && count >= floor {
            out.push_str(&text[copied..m.start()]);
            out.push_str(crate::redact::REDACTED);
            copied = end;
        }
    }
    out.push_str(&text[copied..]);
    out
}
