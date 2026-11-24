//! Failed and passed test ids from a pytest (`-rA`) or `cargo test` log.
use std::collections::BTreeSet;

/// The id of a pytest short-summary line: up to the ` - reason`, brackets kept whole.
fn pytest_id(rest: &str) -> &str {
    let mut depth = 0usize;
    for (at, ch) in rest.char_indices() {
        match ch {
            '[' => depth += 1,
            ']' => depth = depth.saturating_sub(1),
            ' ' if depth == 0 => return &rest[..at],
            _ => {}
        }
    }
    rest
}

fn ids(log: &str, pytest: &[&str], cargo: &str) -> BTreeSet<String> {
    let mut found = BTreeSet::new();
    for line in log.lines() {
        let line = line.trim_end();
        if let Some(rest) = pytest
            .iter()
            .find_map(|word| line.strip_prefix(&format!("{word} ")))
        {
            let id = pytest_id(rest);
            if id.contains("::") || id.ends_with(".py") {
                found.insert(id.to_string());
            }
        } else if let Some(name) = line
            .strip_prefix("test ")
            .and_then(|rest| rest.strip_suffix(&format!(" ... {cargo}")))
        {
            found.insert(name.to_string());
        }
    }
    found
}

pub fn failed_ids(log: &str) -> BTreeSet<String> {
    ids(log, &["FAILED", "ERROR"], "FAILED")
}

pub fn passed_ids(log: &str) -> BTreeSet<String> {
    ids(log, &["PASSED"], "ok")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pytest_short_summary_lines_name_the_failed_ids_without_the_reason() {
        let log = "FAILED tests/test_a.py::test_x - assert 0\nERROR tests/test_b.py::test_y[a b] - boom\nPASSED tests/test_c.py::test_z\n";
        let failed: Vec<_> = failed_ids(log).into_iter().collect();
        assert_eq!(
            failed,
            ["tests/test_a.py::test_x", "tests/test_b.py::test_y[a b]"]
        );
        assert_eq!(
            passed_ids(log).into_iter().collect::<Vec<_>>(),
            ["tests/test_c.py::test_z"]
        );
    }

    #[test]
    fn cargo_result_lines_name_failed_and_passed_tests() {
        let log =
            "test mod::a ... ok\ntest mod::b ... FAILED\ntest result: FAILED. 1 passed; 1 failed\n";
        assert_eq!(failed_ids(log).into_iter().collect::<Vec<_>>(), ["mod::b"]);
        assert_eq!(passed_ids(log).into_iter().collect::<Vec<_>>(), ["mod::a"]);
    }

    #[test]
    fn prose_that_starts_with_the_words_is_not_an_id() {
        assert!(failed_ids("FAILED to connect\nERROR: no\n").is_empty());
    }
}
