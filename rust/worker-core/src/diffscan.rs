//! Deterministic, I/O-free heuristics for test weakening in unified diffs.

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Finding {
    pub path: String,
    pub kind: FindingKind,
    pub line: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FindingKind {
    DeletedTestFile,
    DeletedTestFn,
    SkippedTest,
    RemovedAssert,
    XfailAdded,
}

/// Scan test-file patches in input order, balancing definitions and assertions per file.
pub fn scan_diff(diff: &str) -> Vec<Finding> {
    let mut findings = Vec::new();
    let mut file = Patch::default();
    let mut in_hunk = false;
    for line in diff.lines() {
        if let Some(header) = line.strip_prefix("diff --git ") {
            file.finish(&mut findings);
            file = Patch::default();
            if let Some((old, _)) = header.split_once(" b/") {
                file.path = old.trim_start_matches("a/").to_owned();
            }
            in_hunk = false;
        } else if !in_hunk && line.starts_with("--- ") {
            if file.has_headers {
                file.finish(&mut findings);
                file = Patch::default();
            }
            file.has_headers = true;
            file.path = header_path(&line[4..], "a/");
        } else if !in_hunk && line.starts_with("+++ ") {
            if line[4..].trim() == "/dev/null" {
                file.deleted = Some(line.to_owned());
            } else {
                file.path = header_path(&line[4..], "b/");
            }
        } else if line.starts_with("deleted file mode") {
            file.deleted = Some(line.to_owned());
        } else if line.starts_with("@@") {
            in_hunk = true;
            file.lines.push(('@', String::new()));
        } else if in_hunk {
            if let Some(sign) = line.chars().next().filter(|c| matches!(c, '-' | '+' | ' ')) {
                file.lines.push((sign, line[1..].to_owned()));
            }
        }
    }
    file.finish(&mut findings);
    findings
}

fn header_path(header: &str, prefix: &str) -> String {
    header
        .split('\t')
        .next()
        .unwrap_or(header)
        .trim_start_matches(prefix)
        .to_owned()
}

#[derive(Default)]
struct Patch {
    path: String,
    has_headers: bool,
    deleted: Option<String>,
    lines: Vec<(char, String)>,
}

impl Patch {
    fn finish(&self, out: &mut Vec<Finding>) {
        let name = self.path.rsplit('/').next().unwrap_or("");
        if !(self.path.contains("tests/")
            || (name.starts_with("test_") && name.ends_with(".py"))
            || name.ends_with("_test.py")
            || name.ends_with("_test.rs"))
        {
            return;
        }
        let finding = |kind, line: &str| Finding {
            path: self.path.clone(),
            kind,
            line: line.to_owned(),
        };
        if let Some(line) = &self.deleted {
            out.push(finding(FindingKind::DeletedTestFile, line));
        }
        let mut old_test = false;
        let mut new_test = false;
        let mut removed = Vec::new();
        let mut added = Vec::new();
        let mut asserts = Vec::new();
        let mut added_asserts = 0;
        let mut candidates = Vec::new();
        for (sign, line) in &self.lines {
            if *sign == '@' {
                old_test = false;
                new_test = false;
                continue;
            }
            let text = line.trim();
            if *sign != '+' {
                if let Some(name) = test_fn(text, &mut old_test) {
                    if *sign == '-' {
                        removed.push((name.to_owned(), line));
                    }
                }
            }
            if *sign != '-' {
                if let Some(name) = test_fn(text, &mut new_test) {
                    if *sign == '+' {
                        added.push(name.to_owned());
                    }
                }
            }
            if *sign == '-' && is_assert(text) {
                asserts.push(line);
            }
            if *sign == '+' {
                if is_assert(text) {
                    added_asserts += 1;
                }
                if text.contains("pytest.mark.xfail") {
                    candidates.push(finding(FindingKind::XfailAdded, line));
                } else if [
                    "pytest.mark.skip",
                    "unittest.skip",
                    "#[ignore]",
                    "pytest.skip(",
                ]
                .iter()
                .any(|s| text.contains(s))
                {
                    candidates.push(finding(FindingKind::SkippedTest, line));
                }
            }
        }
        for (name, line) in removed {
            if !added.contains(&name) {
                out.push(finding(FindingKind::DeletedTestFn, line));
            }
        }
        out.extend(candidates);
        // Only the net loss is evidence: an assertion rewrite is not weakening.
        let lost = asserts.len().saturating_sub(added_asserts);
        for line in asserts.into_iter().take(lost) {
            out.push(finding(FindingKind::RemovedAssert, line));
        }
    }
}

fn test_fn<'a>(text: &'a str, rust_test: &mut bool) -> Option<&'a str> {
    let marked = *rust_test || text.contains("#[test]");
    if text.starts_with("#[") {
        *rust_test = marked;
    } else if !text.is_empty() && !text.starts_with("//") {
        *rust_test = false;
    }
    let python = text
        .strip_prefix("async ")
        .unwrap_or(text)
        .strip_prefix("def ");
    let function = if let Some(rest) = python {
        rest.starts_with("test_").then_some(rest)
    } else if marked {
        text.split_once("fn ").map(|(_, rest)| rest)
    } else {
        None
    }?;
    let end = function
        .find(|c: char| !(c.is_alphanumeric() || c == '_'))
        .unwrap_or(function.len());
    (end > 0).then_some(&function[..end])
}

fn is_assert(text: &str) -> bool {
    text.strip_prefix("assert").is_some_and(|rest| {
        rest.is_empty() || rest.starts_with(|c: char| c.is_whitespace() || c == '(' || c == '!')
    }) || text.starts_with("assert_eq!")
        || text.starts_with("assert_ne!")
        || text.starts_with("self.assert")
}

/// Return a stable, single-line check-failure summary, bounded to 300 UTF-8 bytes.
pub fn reason(findings: &[Finding]) -> String {
    let Some(first) = findings.first() else {
        return String::new();
    };
    let label = match first.kind {
        FindingKind::DeletedTestFile => "deleted test file",
        FindingKind::DeletedTestFn => "deleted test function",
        FindingKind::SkippedTest => "skipped test",
        FindingKind::RemovedAssert => "removed assert",
        FindingKind::XfailAdded => "added xfail",
    };
    let suffix = format!(" ({} findings)", findings.len());
    let mut text = format!(
        "test-gaming: {label} in {}",
        first.path.split_whitespace().collect::<Vec<_>>().join(" ")
    );
    let limit = 300 - suffix.len();
    if text.len() > limit {
        let mut end = limit - 3;
        while !text.is_char_boundary(end) {
            end -= 1;
        }
        text.truncate(end);
        text.push_str("...");
    }
    text.push_str(&suffix);
    text
}

#[cfg(test)]
mod tests {
    use super::*;

    fn diff(path: &str, body: &str) -> String {
        format!(
            "diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1,5 +1,5 @@\n{body}\n"
        )
    }
    fn kinds(path: &str, body: &str) -> Vec<FindingKind> {
        scan_diff(&diff(path, body))
            .into_iter()
            .map(|f| f.kind)
            .collect()
    }
    #[test]
    fn empty_diff() {
        assert!(scan_diff("").is_empty());
    }
    #[test]
    fn deleted_file_mode() {
        let d = "diff --git a/tests/x.py b/tests/x.py\ndeleted file mode 100644\n";
        assert_eq!(scan_diff(d)[0].kind, FindingKind::DeletedTestFile);
    }
    #[test]
    fn deleted_file_null() {
        let d = "--- a/test_x.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-def test_x():\n";
        assert!(scan_diff(d)
            .iter()
            .any(|f| f.kind == FindingKind::DeletedTestFile));
    }
    #[test]
    fn deleted_python_fn() {
        assert_eq!(
            kinds("test_x.py", "-def test_x():"),
            vec![FindingKind::DeletedTestFn]
        );
    }
    #[test]
    fn moved_python_fn() {
        assert!(kinds("x_test.py", "-def test_x():\n+def test_x():").is_empty());
    }
    #[test]
    fn deleted_rust_fn() {
        assert_eq!(
            kinds("x_test.rs", "-#[test]\n-fn works() {"),
            vec![FindingKind::DeletedTestFn]
        );
    }
    #[test]
    fn moved_rust_fn() {
        assert!(kinds(
            "x_test.rs",
            "-#[test]\n-fn works() {}\n+#[test]\n+fn works() {}"
        )
        .is_empty());
    }
    #[test]
    fn skips() {
        for line in [
            "@pytest.mark.skip",
            "@pytest.mark.skipif(True)",
            "@unittest.skip('x')",
            "#[ignore]",
            "pytest.skip('x')",
        ] {
            assert_eq!(
                kinds("tests/x.py", &format!("+{line}")),
                vec![FindingKind::SkippedTest]
            );
        }
    }
    #[test]
    fn xfail() {
        assert_eq!(
            kinds("tests/x.py", concat!("+", "@", "pytest.mark.xfail")),
            vec![FindingKind::XfailAdded]
        );
    }
    #[test]
    fn assertions_removed() {
        for line in [
            "assert x",
            "assert_eq!(x, y);",
            "assert!(x);",
            "self.assertEqual(x, y)",
        ] {
            assert_eq!(
                kinds("tests/x.py", &format!("-{line}")),
                vec![FindingKind::RemovedAssert]
            );
        }
    }
    #[test]
    fn assertion_replaced() {
        assert!(kinds("tests/x.py", "-assert x\n+assert y").is_empty());
    }
    #[test]
    fn net_assert_drop() {
        assert_eq!(
            kinds("tests/x.py", "-assert x\n-assert y\n+assert z").len(),
            1
        );
    }
    #[test]
    fn non_test_ignored() {
        assert!(kinds(
            "src/main.py",
            "-assert x\n+pytest.skip('x')\n-def test_x():"
        )
        .is_empty());
    }
    #[test]
    fn multiple_files_isolated() {
        let d = diff("tests/a.py", "-def test_x():")
            + &diff(
                "tests/b.py",
                concat!("+def test_x():\n+", "@", "pytest.mark.xfail"),
            );
        let f = scan_diff(&d);
        assert_eq!(f.len(), 2);
        assert_eq!(f[0].path, "tests/a.py");
        assert_eq!(f[1].path, "tests/b.py");
    }
    #[test]
    fn reason_format() {
        let f = scan_diff(&diff("tests/x.py", "-assert x\n-assert y"));
        assert_eq!(
            reason(&f),
            "test-gaming: removed assert in tests/x.py (2 findings)"
        );
        assert_eq!(reason(&[]), "");
    }
    #[test]
    fn reason_bounded_single_line() {
        let f = Finding {
            path: "é\n".repeat(300),
            kind: FindingKind::SkippedTest,
            line: String::new(),
        };
        let r = reason(&[f]);
        assert!(!r.contains('\n'));
        assert!(r.len() <= 300);
        assert!(!r.is_empty());
    }
    #[test]
    fn context_attribute() {
        assert_eq!(
            kinds("tests/x.rs", " #[test]\n-fn old() {}"),
            vec![FindingKind::DeletedTestFn]
        );
    }
    #[test]
    fn normal_rust_fn_ignored() {
        assert!(kinds("tests/x.rs", "-fn helper() {}").is_empty());
    }
}
