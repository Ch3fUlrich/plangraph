//! The Class-Search line format, shared by the range check and the commit-msg hook.
use regex::Regex;
use std::sync::OnceLock;

/// The form every Class-Search line takes, printed with each refusal.
pub const EXPECTED: &str = "Class-Search: <pattern> -> <n> hits, <m> changed` (`hit` for 1, nothing after `changed`, m <= n)";

/// One valid Class-Search line: `short` is the audited commit's 8-hex prefix of a repair line.
pub struct Record {
    pub short: Option<String>,
    pub hits: usize,
    pub changed: usize,
}

fn format() -> &'static Regex {
    static FORMAT: OnceLock<Regex> = OnceLock::new();
    FORMAT.get_or_init(|| {
        Regex::new(r"^Class-Search: (?:([0-9a-f]{8}) )?(\S.*) -> ([0-9]+) hits?, ([0-9]+) changed$")
            .unwrap()
    })
}

/// The record of a well-formed line with `changed <= hits`, `None` for any other line.
pub fn parse(line: &str) -> Option<Record> {
    let fields = format().captures(line)?;
    let (hits, changed) = (fields[3].parse().ok()?, fields[4].parse().ok()?);
    (changed <= hits).then(|| Record {
        short: fields.get(1).map(|short| short.as_str().to_owned()),
        hits,
        changed,
    })
}

/// The refusal text for a malformed line.
pub fn refusal(line: &str) -> String {
    format!("{line}; expected `{EXPECTED}")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn accepts_hit_and_hits_and_refuses_prose_or_inverted_counts() {
        for line in [
            "Class-Search: x -> 1 hit, 1 changed",
            "Class-Search: x y -> 2 hits, 0 changed",
            "Class-Search: 0123abcd x -> 1 hits, 1 changed",
        ] {
            assert!(parse(line).is_some(), "{line}");
        }
        for line in [
            "Class-Search: x -> 1 hit, 1 changed (note)",
            "Class-Search: x -> 1 hits, 2 changed",
            "Class-Search: x -> 1 hit 1 changed",
            "Class-Search:  -> 1 hit, 1 changed",
        ] {
            assert!(parse(line).is_none(), "{line}");
        }
    }
}
