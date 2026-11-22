//! Contract for the legacy fast-check entry point and the shared public scanner.
#[cfg(test)]
#[path = "../../test_support/python.rs"]
mod test_python;

use std::{
    fs,
    io::Write,
    path::Path,
    process::{Command, Stdio},
};

fn git(repo: &Path, args: &[&str]) {
    let out = Command::new("git")
        .args(args)
        .current_dir(repo)
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
}

fn commit(repo: &Path) {
    git(
        repo,
        &[
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "fixture\n\nPlan-Item: plangraph:TEST\nWriter: codex/test",
        ],
    );
}

fn legacy_scan(repo: &Path) -> std::process::Output {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    test_python::command()
        .arg(root.join("tools/preflight_private_scrub.py"))
        .args(["--base", "HEAD~1", "--head", "HEAD", "--words", "words"])
        .current_dir(repo)
        .output()
        .unwrap()
}

#[test]
fn fast_checks_reject_home_path_without_private_word_hit() {
    let repo = tempfile::tempdir().unwrap();
    git(repo.path(), &["init", "-q"]);
    fs::write(repo.path().join("fixture.txt"), "clean\n").unwrap();
    git(repo.path(), &["add", "."]);
    commit(repo.path());
    let leak = ["/home/", "fixture"].concat();
    fs::write(repo.path().join("fixture.txt"), format!("{leak}\n")).unwrap();
    git(repo.path(), &["add", "."]);
    commit(repo.path());
    fs::write(repo.path().join("words"), "unrelatedprivateword\n").unwrap();
    let out = legacy_scan(repo.path());
    let output = String::from_utf8_lossy(&out.stdout);
    assert_eq!(out.status.code(), Some(1), "{output}");
    assert!(output.contains("fixture.txt:1: home-path"), "{output}");
    assert!(!output.contains(&leak));
}

#[test]
fn public_scrub_callers_share_one_implementation() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    for file in ["tools/hooks/pre-push", "tools/split/make_public.py"] {
        let source = fs::read_to_string(root.join(file)).unwrap();
        assert!(
            source.contains("sc.scan_content("),
            "{file} must delegate to scrub_check"
        );
        assert!(
            !source.contains("sc.scan_line_values("),
            "{file} duplicates scanning"
        );
    }
    let legacy = fs::read_to_string(root.join("tools/preflight_private_scrub.py")).unwrap();
    assert!(legacy.contains("return sc.main(["));
    let preflight = fs::read_to_string(root.join("tools/preflight.py")).unwrap();
    assert!(preflight.contains("scrub = python(\"split/scrub_check.py\""));
    assert!(preflight.contains("scrub.extend([\"--words\", words])"));
    let scanner = fs::read_to_string(root.join("tools/split/scrub_check.py")).unwrap();
    assert!(scanner.contains("found, binary = scan_content("));
    let ci = fs::read_to_string(root.join(".github/workflows/tests.yml")).unwrap();
    assert!(ci.contains("tools/split/scrub_check.py . --rules tools/split/manifest.toml --rules tools/split/scrub_ci.toml"));
}

#[test]
fn committed_scrub_preserves_inherited_lines_and_counts_added_duplicates() {
    let repo = tempfile::tempdir().unwrap();
    git(repo.path(), &["init", "-q"]);
    let leak = ["/home/", "fixture"].concat();
    fs::write(repo.path().join("fixture.txt"), format!("{leak}\n")).unwrap();
    git(repo.path(), &["add", "."]);
    commit(repo.path());
    fs::write(repo.path().join("fixture.txt"), format!("{leak}\nclean\n")).unwrap();
    git(repo.path(), &["add", "."]);
    commit(repo.path());
    fs::write(repo.path().join("words"), "unrelatedprivateword\n").unwrap();
    assert!(legacy_scan(repo.path()).status.success());
    fs::write(
        repo.path().join("fixture.txt"),
        format!("{leak}\nclean\n{leak}\n"),
    )
    .unwrap();
    git(repo.path(), &["add", "fixture.txt"]);
    commit(repo.path());
    let out = legacy_scan(repo.path());
    assert_eq!(out.status.code(), Some(1));
    assert!(String::from_utf8_lossy(&out.stdout).contains("fixture.txt:3: home-path"));
}

#[test]
fn committed_scrub_checks_private_words_in_gitlink_paths() {
    let repo = tempfile::tempdir().unwrap();
    git(repo.path(), &["init", "-q"]);
    fs::write(repo.path().join("fixture.txt"), "clean\n").unwrap();
    git(repo.path(), &["add", "."]);
    commit(repo.path());
    let oid = Command::new("git")
        .args(["rev-parse", "HEAD"])
        .current_dir(repo.path())
        .output()
        .unwrap();
    let oid = String::from_utf8(oid.stdout).unwrap();
    git(
        repo.path(),
        &["update-ref", "refs/remotes/origin/main", oid.trim()],
    );
    git(
        repo.path(),
        &[
            "update-index",
            "--add",
            "--cacheinfo",
            "160000",
            &"f".repeat(40),
            "unrelatedprivateword",
        ],
    );
    commit(repo.path());
    fs::write(repo.path().join("words"), "unrelatedprivateword\n").unwrap();
    let out = legacy_scan(repo.path());
    assert_eq!(out.status.code(), Some(1));
    let output = String::from_utf8_lossy(&out.stdout);
    assert!(output.contains(":0: private-word"), "{output}");
    assert!(!output.contains("unrelatedprivateword"));
    let head = Command::new("git")
        .args(["rev-parse", "HEAD"])
        .current_dir(repo.path())
        .output()
        .unwrap();
    let head = String::from_utf8(head.stdout).unwrap();
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let mut hook = test_python::command()
        .arg(root.join("tools/hooks/pre-push"))
        .arg("origin")
        .current_dir(repo.path())
        .env("PLANGRAPH_SCRUB_WORDS", repo.path().join("words"))
        .env_remove("PLANGRAPH_SCRUB_RULES")
        .env_remove("PREFLIGHT_SKIP")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    writeln!(
        hook.stdin.take().unwrap(),
        "refs/heads/topic {} refs/heads/topic {}",
        head.trim(),
        oid.trim()
    )
    .unwrap();
    let out = hook.wait_with_output().unwrap();
    let output = format!(
        "{}{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    );
    assert_eq!(out.status.code(), Some(1), "{output}");
    assert!(output.contains(":0: private-word"), "{output}");
    assert!(!output.contains("unrelatedprivateword"));
}
