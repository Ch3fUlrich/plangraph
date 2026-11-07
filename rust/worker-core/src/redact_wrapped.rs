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
        let limit = super::redact_tokens::SPECS[index].wrapped_limit();
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
            if count >= limit {
                break;
            }
            let next = if bytes.get(end..end + 2) == Some(b"\r\n") {
                end + 2
            } else if bytes.get(end) == Some(&b'\n') {
                end + 1
            } else {
                break;
            };
            if count >= floor {
                let mut line_end = next;
                while line_end < bytes.len() && allowed(bytes[line_end]) {
                    line_end += 1;
                }
                let line = &bytes[next..line_end];
                // Consume whole allowed-byte lines, even single words. Stop at
                // MAX_WRAPPED_BODY (the table's longest wrapped-token budget),
                // so one key cannot consume an unlimited number of lines.
                if count + line.len() > limit
                    || !(line_end == bytes.len()
                        || bytes.get(line_end) == Some(&b'\n')
                        || bytes.get(line_end..line_end + 2) == Some(b"\r\n"))
                {
                    break;
                }
            }
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
    fn all_prefixes_consume_short_letter_lines_with_a_bound() {
        for (index, left, right) in [
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
        ] {
            let floor = crate::redact_tokens::SPECS[index].floor;
            for len in [floor, floor + 4] {
                for nl in ["\n", "\r\n"] {
                    for tail in ["A", "AA"] {
                        for ending in [
                            String::new(),
                            format!("{nl}kept prose"),
                            format!("{nl}B{nl}kept prose"),
                        ] {
                            let input =
                                format!("{left}{right}{}{nl}{tail}{ending}", "A".repeat(len));
                            let expected = if ending.is_empty() {
                                "[REDACTED]"
                            } else {
                                "[REDACTED]\nkept prose"
                            };
                            assert_eq!(redact_wrapped(&input).replace("\r\n", "\n"), expected);
                        }
                    }
                }
            }
        }
    }

    #[test]
    fn join_budget_and_disallowed_lines_stop_consumption() {
        let prefix = ["s", "k-"].concat();
        let limit = crate::redact_tokens::MAX_WRAPPED_BODY;
        for nl in ["\n", "\r\n"] {
            let input = format!("{prefix}{}{nl}a{nl}b", "a".repeat(limit - 1));
            assert_eq!(redact_wrapped(&input), format!("[REDACTED]{nl}b"));
            let input = format!("{prefix}a{nl}a word");
            assert_eq!(redact_wrapped(&input), input);
        }
    }

    #[test]
    fn prefix_capture_indices_identify_specs() {
        assert_eq!(
            prefix_regex().captures_len(),
            crate::redact_tokens::SPECS.len() + 1
        );
    }
}
