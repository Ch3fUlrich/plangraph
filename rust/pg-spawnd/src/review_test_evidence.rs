/// Return the last standalone test report, ignoring Markdown examples.
pub(crate) fn tests_line(text: &str) -> Option<&str> {
    test_report(text).map(|(_, body)| body)
}

pub(crate) fn required(leg: &serde_json::Value) -> bool {
    leg["id"] == "claude-sonnet-5-5-high"
        || (leg["harness"] == "claude" && leg["model"] == "claude-sonnet-5-5-high")
}

fn test_report(text: &str) -> Option<(std::ops::Range<usize>, &str)> {
    let mut fence: Option<(u8, usize)> = None;
    let mut report = None;
    let mut offset = 0;
    for segment in text.split_inclusive('\n') {
        let start = offset;
        offset += segment.len();
        let raw = segment.trim_end_matches(['\r', '\n']);
        let line = raw.trim_start_matches(' ');
        if raw.len() - line.len() >= 4 || line.starts_with('\t') {
            continue;
        }
        if let Some(marker @ (b'`' | b'~')) = line.bytes().next() {
            let width = line.bytes().take_while(|&byte| byte == marker).count();
            if let Some((open, minimum)) = fence {
                if marker == open && width >= minimum && line[width..].trim().is_empty() {
                    fence = None;
                }
            } else if width >= 3 {
                fence = Some((marker, width));
            }
            continue;
        }
        if fence.is_none() {
            if let Some(body) = line.strip_prefix("TESTS:") {
                report = Some((start..start + raw.len(), body.trim()));
            }
        }
    }
    report
}

pub(crate) fn set_tests_line(mut text: String, line: &str) -> String {
    if let Some((range, _)) = test_report(&text) {
        text.replace_range(range, line);
    } else {
        text.insert_str(0, &format!("{line}\n"));
    }
    text
}

pub(crate) fn reported(text: &str) -> Option<&str> {
    tests_line(text).filter(|report| {
        let lower = report.to_ascii_lowercase();
        if lower.contains("requires approval")
            || lower.contains("not permitted")
            || lower
                .split(|c: char| !c.is_ascii_alphabetic())
                .any(|word| matches!(word, "denied" | "disallowed"))
        {
            return false;
        }
        let mut words = report
            .split(|c: char| c == ':' || c.is_whitespace())
            .filter(|word| !word.is_empty());
        let mut first = words.next().map(str::to_ascii_lowercase);
        if matches!(first.as_deref(), Some("i" | "we")) {
            first = words.next().map(str::to_ascii_lowercase);
        }
        if matches!(first.as_deref(), Some("can" | "could" | "did"))
            && words
                .next()
                .is_some_and(|word| word.eq_ignore_ascii_case("not"))
        {
            return false;
        }
        first.is_some_and(|word| {
            !matches!(
                word.as_str(),
                "none"
                    | "no"
                    | "not"
                    | "couldn't"
                    | "couldn’t"
                    | "cannot"
                    | "can't"
                    | "can’t"
                    | "didn't"
                    | "didn’t"
                    | "unable"
                    | "unavailable"
                    | "skipped"
                    | "untested"
            )
        })
    })
}

#[cfg(test)]
mod tests {
    use super::reported;

    #[test]
    fn only_a_standalone_nonempty_tests_line_reports_tests() {
        for text in [
            "VERDICT: ACCEPT\n",
            "TESTS: none: Bash denied\n",
            "TESTS: pytest -q tests/test_fixture.py — permission denied\n",
            "TESTS: cargo test -p fixture (requires approval)\n",
            "TESTS: NONE\n",
            "TESTS: not run (denied)\n",
            "TESTS: could not run any tests\n",
            "TESTS: I couldn't run any tests (Bash requires approval)\n",
            "TESTS: We did not run pytest\n",
            "TESTS: Unable to run pytest\n",
            "TESTS: no tests executed\n",
            "TESTS: Cannot run Cargo tests\n",
            "TESTS:   \n",
            "> TESTS: pytest fixture.py — passed\n",
            "```text\nTESTS: cargo test -p fixture — passed\n```\n",
            "~~~~text\n~~~\nTESTS: cargo test -p fixture — passed\n~~~~\n",
            "    TESTS: pytest fixture.py — passed\n",
            "TESTS: pytest fixture.py — passed\nTESTS: none: denied\n",
        ] {
            assert_eq!(reported(text), None, "{text}");
        }
        assert_eq!(
            reported(
                "FILES READ: lib.rs\nTESTS: cargo test -p fixture — 1 passed\nVERDICT: ACCEPT"
            ),
            Some("cargo test -p fixture — 1 passed")
        );
        assert_eq!(
            reported("TESTS: I ran pytest fixture.py — 1 failed"),
            Some("I ran pytest fixture.py — 1 failed")
        );
    }
}
