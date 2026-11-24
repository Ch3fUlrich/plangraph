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

/// The id without xdist's `@group` suffix, which follows the last `]` or `::` (the test id's own `@` stays).
fn canonical(id: &str) -> &str {
    let floor = id.rfind(']').max(id.rfind("::")).map_or(0, |at| at + 1);
    match id.rfind('@') {
        Some(at) if at >= floor => &id[..at],
        _ => id,
    }
}

fn ids(log: &str, pytest: &[&str], cargo: &str) -> BTreeSet<String> {
    let mut found = BTreeSet::new();
    for line in log.lines() {
        let line = line.trim_end();
        if let Some(rest) = pytest
            .iter()
            .find_map(|word| line.strip_prefix(&format!("{word} ")))
        {
            let id = canonical(pytest_id(rest));
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
    fn an_xdist_group_suffix_is_not_part_of_the_canonical_test_id() {
        let log = "FAILED tests/t.py::test_red@keeper - x\nFAILED tests/t.py::test_p[a@b] - y\nFAILED tests/t.py::test_q[a]@grp\n";
        let failed: Vec<_> = failed_ids(log).into_iter().collect();
        // Only an @ after the last `]` or `::` is a group suffix; one inside a parameter id stays.
        assert_eq!(
            failed,
            [
                "tests/t.py::test_p[a@b]",
                "tests/t.py::test_q[a]",
                "tests/t.py::test_red"
            ]
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
