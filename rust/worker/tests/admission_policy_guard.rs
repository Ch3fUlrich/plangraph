//! Repository guard: the unrestricted helper belongs only in test code.
#[test]
fn unrestricted_policy_is_test_only() {
    fn walk(dir: &std::path::Path) {
        for entry in std::fs::read_dir(dir).unwrap() {
            let path = entry.unwrap().path();
            if path.is_dir() {
                if !matches!(
                    path.file_name().and_then(|s| s.to_str()),
                    Some("target" | ".git")
                ) {
                    walk(&path);
                }
            } else if path.extension().is_some_and(|s| s == "rs") {
                let text = std::fs::read_to_string(&path).unwrap();
                let is_tests = path.components().any(|c| c.as_os_str() == "tests")
                    || path
                        .file_name()
                        .unwrap()
                        .to_string_lossy()
                        .ends_with("_tests.rs");
                for (offset, _) in text.match_indices("allow_all_for_tests(") {
                    if is_tests {
                        continue;
                    }
                    let before = &text[..offset];
                    assert!(before.contains("#[cfg(test)]")
                        || before.contains("#[cfg(all(test, unix))]")
                        || (path.ends_with("card_allowlist.rs") && before.contains("#[cfg(all(debug_assertions, any(test, feature = \"test-support\")))]")),
                        "unrestricted policy outside tests: {}", path.display());
                }
            }
        }
    }
    walk(
        std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .unwrap()
            .parent()
            .unwrap(),
    );
}
