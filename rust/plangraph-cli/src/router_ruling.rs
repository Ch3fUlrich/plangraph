//! `plangraph router ruling-row ROW.txt`: a decision row or proposal that states a cause, size
//! or time of this system must carry a `Measured:` line with our own numbers (rule 4.02).
//!
//! The statement is a label (`cause:`, `size:`, `time:`) anywhere in the text. The evidence is a
//! `Measured:` field (the label starts a line, a table cell or a sentence; `Unmeasured:` is not
//! one) of the form `Measured: SOURCE ... ANCHOR ... METRIC VALUE`, up to the end of its line or
//! table cell: the first word (after an optional "our") is one of our own data sources (keeper,
//! telemetry, ledger, ci-runs, journal, decisions-log, sqlite); a token identifies the result (a
//! date or date range, a decision id, a pull request or a run id); and a metric word stands
//! directly before a signed number with an optional attached unit (`median 250ms`,
//! `delta -6.5 jobs/hour`, `trains=63`). A time of day, an id or an HTTP status is not a value.
//! The field must be a completed measurement of ours: not a failed or pending query, and not
//! another company's system.

const LABELS: [&str; 3] = ["cause", "size", "time"];
const MEASURED: &str = "measured:";
/// Our own data sources: the first word of a `Measured:` field.
const OWN_SOURCES: [&str; 7] = [
    "keeper",
    "telemetry",
    "ledger",
    "ci-runs",
    "journal",
    "decisions-log",
    "sqlite",
];

/// Phrases of a measurement that found nothing, failed or is not finished (a pending queue of
/// trains is a measured quantity; a pending query is not).
const EMPTY: [&str; 14] = [
    "no result",
    "no data",
    "n/a",
    "not yet",
    "nothing found",
    "none found",
    "query failed",
    "request failed",
    "timed out",
    "unreachable",
    "pending query",
    "query pending",
    "query is pending",
    "still running",
];

/// Another company's or tool's system: its numbers are not ours, whatever source word they carry.
const EXTERNAL: [&str; 12] = [
    "github's",
    "merge queue",
    "merge-queue",
    "gitlab",
    "zuul",
    "chromium",
    "google",
    "uber",
    "netflix",
    "other compan",
    "industry",
    "best practice",
];

/// Words before a number that name no metric (prepositions, status and error codes, identifiers).
const NOT_METRIC: [&str; 27] = [
    "for", "on", "at", "of", "in", "to", "and", "or", "with", "by", "from", "as", "http", "status",
    "code", "error", "errno", "since", "until", "is", "was", "than", "id", "pr", "run", "key",
    "ref",
];

/// Whether one token identifies a result: a date or date range, a decision id, a pull request
/// number or a run id.
fn anchor(token: &str) -> bool {
    let b = token.as_bytes();
    let date = b.len() >= 10
        && b[..10].iter().enumerate().all(|(i, c)| {
            if i == 4 || i == 7 {
                *c == b'-'
            } else {
                c.is_ascii_digit()
            }
        });
    date || token.starts_with("run-")
        || token
            .strip_prefix("d-")
            .or_else(|| token.strip_prefix('#'))
            .is_some_and(|n| !n.is_empty() && n.bytes().all(|c| c.is_ascii_digit()))
}

/// A signed number with an optional attached unit (`14`, `-6.5`, `250ms`, `12%`, `3jobs/hour`); a
/// time of day, a date or an id is not a value.
fn value(token: &str) -> bool {
    let rest = token.trim_start_matches(['+', '-', '<', '>', '~']);
    let digits = rest.bytes().take_while(u8::is_ascii_digit).count();
    if digits == 0 {
        return false;
    }
    let mut rest = &rest[digits..];
    if let Some(fraction) = rest.strip_prefix('.') {
        let places = fraction.bytes().take_while(u8::is_ascii_digit).count();
        if places == 0 {
            return false;
        }
        rest = &fraction[places..];
    }
    let unit = rest.strip_prefix('%').unwrap_or(rest);
    unit.split('/')
        .all(|part| part.bytes().all(|c| c.is_ascii_alphabetic()))
}

/// A word that can name a measured quantity right before its value.
fn metric(token: &str) -> bool {
    token.len() >= 2
        && token
            .bytes()
            .all(|c| c.is_ascii_alphabetic() || c == b'_' || c == b'-')
        && !anchor(token)
        && !NOT_METRIC.contains(&token)
        && !OWN_SOURCES.contains(&token)
}

/// Whether the text of one `Measured:` field is evidence: a completed measurement that starts with
/// one of our sources, holds an anchor and a metric with its value, and names no other company's
/// system.
fn measured_field(cell: &str) -> bool {
    if EMPTY
        .iter()
        .chain(&EXTERNAL)
        .any(|phrase| cell.contains(phrase))
    {
        return false;
    }
    let tokens: Vec<String> = cell
        .split(|c: char| c.is_whitespace() || ",;()".contains(c))
        .map(|t| t.trim_matches(|c: char| !(c.is_alphanumeric() || "#-+<>~%.".contains(c))))
        // `metric=value` is two tokens: the metric and its value.
        .flat_map(|t| t.split('='))
        .filter(|t| !t.is_empty())
        .map(str::to_owned)
        .collect();
    let first = tokens.iter().find(|t| !matches!(t.as_str(), "our" | "the"));
    let source = first.is_some_and(|t| OWN_SOURCES.contains(&t.as_str()));
    let anchored = tokens.iter().any(|t| anchor(t));
    let measured = tokens
        .windows(2)
        .any(|pair| metric(&pair[0]) && !anchor(&pair[1]) && value(&pair[1]));
    source && anchored && measured
}

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
        own_field && measured_field(cell)
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
            "| D-1 | demo | y | router | cause: CI queue. Measured: keeper query runs 2026-11-20..24, 41 of 63 red trains waited 30 min |",
            "cause: queue\nMeasured: keeper query 2026-11-24 counted 14 pending trains",
            "cause: queue\nMeasured: telemetry 2026-11-24 median 250ms",
            "cause: queue\nMeasured: telemetry 2026-11-24 throughput delta -6.5 jobs/hour",
            "cause: queue\nMeasured: ledger 2026-11-24 trains=63 red=41",
            "cause: queue\nMeasured: our telemetry 2026-11-24..26 median 6.5 parts, 12% red",
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
            "cause: slow CI\nMeasured: GitHub CI ran 42 trains",
            "cause: slow CI\nMeasured: Our keeper query ran on 2026-11-24; no results yet",
            "cause: slow CI\nMeasured: keeper query 2026-11-24",
            "cause: slow CI\nMeasured: keeper query, 14 trains, no data on 2026-11-24",
            "cause: slow CI\nMeasured: GitHub CI ran 42 trains on 2026-11-24",
            "cause: slow CI\nMeasured: keeper query started at 09:30 on 2026-11-24",
            "cause: slow CI\nMeasured: ci 41 runs 2026-11-24",
            "cause: slow CI\nMeasured: keeper query failed, HTTP status 503 on 2026-11-24",
            "cause: slow CI\nMeasured: keeper pending query for 14 projects on 2026-11-24",
            "cause: slow CI\nMeasured: ci-runs for GitHub's own merge queue counted 42 trains on 2026-11-24",
            "cause: slow CI\nMeasured: keeper 2026-11-24 id 7 at 09:30",
            "cause: slow CI\nMeasured: keeper 2026-11-24 run-12 d-6744",
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
