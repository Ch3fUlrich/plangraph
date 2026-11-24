//! `plangraph router ruling-row ROW.txt`: a decision row or proposal that states a cause, size
//! or time of this system must carry a `Measured:` line with our own numbers (rule 4.02).
//!
//! The statement is a label (`cause:`, `size:`, `time:`) anywhere in the text. The evidence is a
//! `Measured:` field (the label starts a line, a table cell or a sentence; `Unmeasured:` is not
//! one) whose text, up to the end of its line or table cell, holds a number and names one of our
//! own data sources (keeper, telemetry, ledger, CI, a query, journal or log). Other companies'
//! practice never satisfies it: a number beside only a company name has no source of ours.

const LABELS: [&str; 3] = ["cause", "size", "time"];
const MEASURED: &str = "measured:";
/// Words that name data of this system. Matched as whole words of the Measured text.
const OWN_SOURCES: [&str; 12] = [
    "keeper",
    "telemetry",
    "ledger",
    "ci",
    "query",
    "sqlite",
    "journal",
    "jsonl",
    "decisions-log",
    "events",
    "db",
    "pg",
];

/// Positions of `label:` as a whole word (so `runtime:` and `timeout:` do not count).
fn labelled(lower: &str, label: &str) -> bool {
    let mut from = 0;
    while let Some(at) = lower[from..].find(label) {
        let start = from + at;
        let end = start + label.len();
        let word_before = lower[..start]
            .chars()
            .next_back()
            .is_some_and(|c| c.is_alphanumeric() || c == '_' || c == '-');
        let colon = lower[end..]
            .trim_start_matches([' ', '\t'])
            .starts_with(':');
        if !word_before && colon {
            return true;
        }
        from = end;
    }
    false
}

/// The reason `text` is refused, or `None` when it states no cause, size or time, or when it
/// carries a `Measured:` line with numbers.
pub fn ruling_refusal(text: &str) -> Option<&'static str> {
    let lower = text.to_lowercase();
    if !LABELS.iter().any(|label| labelled(&lower, label)) {
        return None;
    }
    let evidence = lower.match_indices(MEASURED).any(|(at, _)| {
        // A distinct field: not the tail of a longer word such as `unmeasured:`.
        let own_field = !lower[..at]
            .chars()
            .next_back()
            .is_some_and(|c| c.is_alphanumeric() || c == '_' || c == '-');
        let rest = &lower[at + MEASURED.len()..];
        let cell = rest.split(['\n', '|']).next().unwrap_or("");
        let words = || cell.split(|c: char| !(c.is_alphanumeric() || c == '-'));
        own_field
            && cell.bytes().any(|b| b.is_ascii_digit())
            && words().any(|word| OWN_SOURCES.contains(&word))
    });
    (!evidence).then_some(
        "ruling_no_measured: a cause, size or time needs a 'Measured:' line with our own numbers and their source",
    )
}

/// `plangraph router ruling-row ROW.txt`: prints `accept`, or `refuse: <reason>` with exit 2.
pub fn run(path: &str) -> i32 {
    match std::fs::read_to_string(path) {
        Ok(text) => match ruling_refusal(&text) {
            None => {
                println!("accept");
                0
            }
            Some(reason) => {
                println!("refuse: {reason}");
                2
            }
        },
        Err(error) => {
            eprintln!("router ruling-row: {path}: {error}");
            1
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_cause_size_or_time_without_a_measured_line_is_refused() {
        for row in [
            "| D-1 | demo | merge trains stay small | router | cause: queue practice at other companies |",
            "size: at most 4 parts per train (GitHub merge queue, Zuul)",
            "Decision: no big trains\nTime: 20 minutes per train",
            "| D-2 | demo | x | router | CAUSE : slow CI |",
        ] {
            assert!(ruling_refusal(row).is_some(), "{row}");
        }
    }

    #[test]
    fn a_measured_line_with_numbers_and_a_source_passes() {
        for row in [
            "| D-1 | demo | y | router | cause: CI queue. Measured: keeper query runs 2026-11-20..24, 41 of 63 red trains waited >30 min |",
            "size: 6 parts\nMeasured: ledger rows 2026-11-01..24, median 6 parts per train, 14 trains",
        ] {
            assert_eq!(ruling_refusal(row), None, "{row}");
        }
    }

    #[test]
    fn a_measured_line_without_numbers_or_source_does_not_pass() {
        for row in [
            "cause: slow CI\nMeasured: see other companies",
            "cause: slow CI\nMeasured: 41",
            "cause: slow CI | Measured: |",
            "cause: slow CI\nMeasured:\n41 runs in the ledger",
            "cause: slow CI\nUnmeasured: 2 estimates from the keeper",
            "cause: slow CI\nMeasured: GitHub 42 trains",
            "size: 4 parts\nMeasured: Zuul and Chromium CQ practice, 5 trains",
        ] {
            assert!(ruling_refusal(row).is_some(), "{row}");
        }
    }

    #[test]
    fn rows_without_such_wording_need_no_measured_line() {
        for row in [
            "| D-3 | demo | GO #12 GO-HEAD abc | router | parts 2: ci green |",
            "runtime: fine, timeout: 30 and the date 08:05Z",
            "grant lane writer-1 scope demo:A",
        ] {
            assert_eq!(ruling_refusal(row), None, "{row}");
        }
    }
}
