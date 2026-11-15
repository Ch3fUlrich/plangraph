//! Minimum review evidence: a changed path or an explicit file:line citation.
use std::{path::Path, sync::OnceLock};

pub(crate) fn diff_paths(cwd: &Path, base: Option<&str>, head: &str) -> Vec<String> {
    if head.starts_with('-') || base.is_some_and(|s| s.starts_with('-')) {
        return Vec::new();
    }
    let output = if let Some(base) = base {
        crate::cli_review::git(
            cwd,
            &[
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--name-only",
                "--no-renames",
                "-z",
                base,
                head,
                "--",
            ],
        )
    } else {
        crate::cli_review::git(
            cwd,
            &[
                "diff-tree",
                "--root",
                "--no-commit-id",
                "--name-only",
                "--no-renames",
                "-r",
                "-z",
                head,
                "--",
            ],
        )
    };
    output
        .unwrap_or_default()
        .split('\0')
        .filter(|s| !s.is_empty())
        .map(str::to_owned)
        .collect()
}

pub(crate) fn names_file(text: &str, paths: &[String]) -> bool {
    // Boundaries prevent src/lib.rs from matching unrelated/src/lib.rs.bak.
    let path_char = |c: char| c.is_alphanumeric() || "_./-\\".contains(c);
    if paths.iter().filter(|p| !p.is_empty()).any(|path| {
        text.match_indices(path.as_str()).any(|(start, _)| {
            !text[..start].chars().next_back().is_some_and(path_char) && {
                let after = &text[start + path.len()..];
                !after.chars().next().is_some_and(path_char)
                    || after.strip_prefix('.').is_some_and(|tail| {
                        tail.is_empty() || tail.starts_with(char::is_whitespace)
                    })
            }
        })
    }) {
        return true;
    }
    static FILE_LINE: OnceLock<regex::Regex> = OnceLock::new();
    FILE_LINE
        .get_or_init(|| {
            regex::Regex::new(r"(?:^|[^\w./-])[\p{L}_./][\w./-]*:[1-9][0-9]*(?:$|[^\w])").unwrap()
        })
        .is_match(text)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn cites_changed_paths_or_file_lines_with_boundaries() {
        let paths = vec!["src/lib.rs".into(), "README".into(), "a file.txt".into()];
        for text in [
            "Reviewed `src/lib.rs`.",
            "Reviewed src/lib.rs.",
            "README looks correct.",
            "Reviewed a file.txt",
            "See other/test.rs:42.",
            "Reviewed Makefile:12.",
        ] {
            assert!(names_file(text, &paths), "{text}");
        }
        for text in [
            "FINDING none\nVERDICT: ACCEPT",
            "unrelated/src/lib.rs",
            "src/lib.rs.bak",
            "other/test.rs",
            "test.rs:0",
            "test.rs:12oops",
            "Tests began at 04:22 UTC.\nVERDICT: ACCEPT",
        ] {
            assert!(!names_file(text, &paths), "{text}");
        }
    }
}
