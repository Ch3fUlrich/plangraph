#![cfg(unix)]
//! Exercise the every-PR gate and repository ignore rules through real Git diffs.
use std::path::Path;
use std::process::{Command, Output};

fn git(root: &Path, args: &[&str]) -> Output {
    pg_worker_core::git_test_support::command(root)
        .args(args)
        .env("GIT_AUTHOR_NAME", "Test")
        .env("GIT_AUTHOR_EMAIL", "test@example.invalid")
        .env("GIT_COMMITTER_NAME", "Test")
        .env("GIT_COMMITTER_EMAIL", "test@example.invalid")
        .output()
        .unwrap()
}

#[test]
fn ignored_notes_and_forced_or_renamed_additions() {
    let source = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    for (name, rename) in [
        ("STATUS-x.txt", false),
        ("nested/STATUS-x.txt", false),
        ("nested/STATUS-renamed.txt", true),
        ("nested/STATUS-line\nbreak.txt", false),
    ] {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        assert!(git(root, &["init", "-b", "main"]).status.success());
        assert!(git(root, &["config", "core.hooksPath", "/dev/null"])
            .status
            .success());
        std::fs::copy(source.join(".gitignore"), root.join(".gitignore")).unwrap();
        std::fs::write(root.join("ordinary.txt"), "existing file\n").unwrap();
        assert!(git(root, &["add", "."]).status.success());
        assert!(git(root, &["commit", "-m", "base"]).status.success());
        assert!(git(root, &["branch", "base"]).status.success());
        std::fs::create_dir(root.join("nested")).unwrap();
        std::fs::write(root.join(name), "existing file\n").unwrap();
        let status = git(root, &["status", "--porcelain"]);
        assert!(status.stdout.is_empty(), "{status:?}");
        assert!(git(root, &["add", "-f", name]).status.success());
        if rename {
            assert!(git(root, &["rm", "ordinary.txt"]).status.success());
        }
        assert!(
            git(root, &["commit", "-m", "note\n\nChangelog: skip (fixture)"])
                .status
                .success()
        );
        let result = Command::new("python3")
            .arg(source.join("tools/check_changelog_docs.py"))
            .args(["--base", "base", "--cwd", root.to_str().unwrap()])
            .output()
            .unwrap();
        assert!(!result.status.success(), "{result:?}");
        let stdout = String::from_utf8_lossy(&result.stdout);
        assert!(
            stdout.contains(&format!("writer status file must not be committed: {name}")),
            "{stdout}"
        );
        assert!(git(root, &["rm", name]).status.success());
        assert!(git(
            root,
            &["commit", "-m", "remove\n\nChangelog: skip (fixture)"]
        )
        .status
        .success());
        let result = Command::new("python3")
            .arg(source.join("tools/check_changelog_docs.py"))
            .args(["--base", "HEAD~1", "--cwd", root.to_str().unwrap()])
            .output()
            .unwrap();
        assert!(result.status.success(), "{result:?}");
        let full_range = Command::new("python3")
            .arg(source.join("tools/check_changelog_docs.py"))
            .args(["--base", "base", "--cwd", root.to_str().unwrap()])
            .output()
            .unwrap();
        assert!(!full_range.status.success());
        assert!(String::from_utf8_lossy(&full_range.stdout)
            .contains(&format!("writer status file must not be committed: {name}")));
    }
}
