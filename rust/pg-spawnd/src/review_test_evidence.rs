/// Return the last standalone test report, ignoring Markdown examples.
pub(crate) fn tests_line(text: &str) -> Option<&str> {
    let mut fence: Option<(u8, usize)> = None;
    let mut report = None;
    for raw in text.lines() {
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
                report = Some(body.trim());
            }
        }
    }
    report
}

pub(crate) fn reported(text: &str) -> Option<&str> {
    tests_line(text).filter(|report| {
        let first = report.split(|c: char| c == ':' || c.is_whitespace()).next();
        !report.is_empty() && !first.is_some_and(|word| word.eq_ignore_ascii_case("none"))
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
            "TESTS: NONE\n",
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
    }
}
