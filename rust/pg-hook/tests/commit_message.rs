#![cfg(unix)]
use std::path::Path;
use std::process::Output;

fn git(root: &Path, args: &[&str]) -> Output {
    pg_worker_core::git_test_support::command(root)
        .args(args)
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .env("GIT_AUTHOR_NAME", "Test")
        .env("GIT_AUTHOR_EMAIL", "test@example.invalid")
        .env("GIT_COMMITTER_NAME", "Test")
        .env("GIT_COMMITTER_EMAIL", "test@example.invalid")
        .output()
        .unwrap()
}

fn repo() -> tempfile::TempDir {
    let dir = tempfile::tempdir().unwrap();
    assert!(git(dir.path(), &["init", "-b", "main"]).status.success());
    let tools = dir.path().join("tools");
    std::fs::create_dir(&tools).unwrap();
    let hooks = tools.join("hooks");
    std::fs::create_dir(&hooks).unwrap();
    std::fs::copy(env!("CARGO_BIN_EXE_commit-msg"), hooks.join("commit-msg")).unwrap();
    assert!(git(
        dir.path(),
        &["config", "core.hooksPath", hooks.to_str().unwrap()]
    )
    .status
    .success());
    std::fs::copy(
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tools/ci_commit_gate.py"),
        tools.join("ci_commit_gate.py"),
    )
    .unwrap();
    dir
}

#[test]
fn real_commits_share_the_ci_trailer_rules() {
    let dir = repo();
    for (message, diagnostic) in [
        ("x", "missing Writer trailer"),
        (
            "x\n\nWriter: codex\n# retained body",
            "missing Writer trailer",
        ),
        ("x\n\nWriter: ", "malformed Writer trailer"),
        ("x\n\nHand-edit: reason", "malformed Hand-Edit trailer"),
        ("x\n\nWriter: codex\nChangelog: skip", "malformed Changelog"),
        (
            "x\n\nWriter: codex\nChangelog: skip ( )",
            "malformed Changelog",
        ),
        (
            "x\n\nWriter: codex\nChangelog: skip ((nested))",
            "malformed Changelog",
        ),
        ("x\n\nWriter: codex\n\nbody", "missing Writer trailer"),
    ] {
        let output = git(dir.path(), &["commit", "--allow-empty", "-m", message]);
        assert!(!output.status.success(), "{message}");
        assert!(
            String::from_utf8_lossy(&output.stderr).contains(diagnostic),
            "{output:?}"
        );
    }
    for message in [
        "x\n\nWriter: codex",
        "x\n\nHand-Edit: typo",
        "x\n\nWriter: codex\nChangelog: skip (test fixture)",
    ] {
        let output = git(dir.path(), &["commit", "--allow-empty", "-m", message]);
        assert!(output.status.success(), "{output:?}");
    }
}

#[test]
fn actual_merge_commit_needs_no_trailers() {
    let dir = repo();
    for args in [
        vec!["commit", "--allow-empty", "-m", "base\n\nWriter: codex"],
        vec!["checkout", "-b", "other"],
        vec!["commit", "--allow-empty", "-m", "other\n\nWriter: codex"],
        vec!["checkout", "main"],
        vec!["merge", "--no-ff", "other", "-m", "merge without trailers"],
    ] {
        let output = git(dir.path(), &args);
        assert!(output.status.success(), "{output:?}");
    }
    assert_eq!(
        String::from_utf8_lossy(&git(dir.path(), &["rev-list", "--parents", "-1", "HEAD"]).stdout)
            .split_whitespace()
            .count(),
        3
    );
}

#[test]
fn missing_policy_fails_closed() {
    let dir = repo();
    std::fs::remove_file(dir.path().join("tools/ci_commit_gate.py")).unwrap();
    assert!(!git(
        dir.path(),
        &["commit", "--allow-empty", "-m", "x\n\nWriter: codex"]
    )
    .status
    .success());
}

#[test]
fn editor_comments_do_not_hide_valid_trailers() {
    let dir = repo();
    for (key, comment_char) in [
        ("core.commentChar", "#"),
        ("core.commentChar", ";"),
        ("core.commentChar", "auto"),
        ("core.commentString", "//"),
    ] {
        assert!(git(dir.path(), &["config", key, comment_char])
            .status
            .success());
        std::fs::write(dir.path().join("staged.txt"), comment_char).unwrap();
        assert!(git(dir.path(), &["add", "staged.txt"]).status.success());
        let output = pg_worker_core::git_test_support::command(dir.path())
            .args([
                "commit",
                "--allow-empty",
                "--edit",
                "--verbose",
                "-m",
                "editor commit\n\n# body forces auto to choose another prefix\n\nWriter: codex",
            ])
            .env("GIT_EDITOR", "true")
            .env("GIT_CONFIG_NOSYSTEM", "1")
            .env("GIT_CONFIG_GLOBAL", "/dev/null")
            .env("GIT_AUTHOR_NAME", "Test")
            .env("GIT_AUTHOR_EMAIL", "test@example.invalid")
            .env("GIT_COMMITTER_NAME", "Test")
            .env("GIT_COMMITTER_EMAIL", "test@example.invalid")
            .output()
            .unwrap();
        assert!(output.status.success(), "{output:?}");
    }
}

#[test]
fn installed_validator_works_in_an_older_linked_worktree() {
    let dir = repo();
    assert!(git(
        dir.path(),
        &["commit", "--allow-empty", "-m", "base\n\nWriter: codex"]
    )
    .status
    .success());
    let linked = tempfile::tempdir().unwrap();
    assert!(git(
        dir.path(),
        &[
            "worktree",
            "add",
            "--detach",
            linked.path().to_str().unwrap()
        ]
    )
    .status
    .success());
    // No gate script exists in this older worktree. The installed pair owns policy.
    assert!(!linked.path().join("tools/ci_commit_gate.py").exists());
    let output = git(
        linked.path(),
        &["commit", "--allow-empty", "-m", "x\n\nWriter: codex"],
    );
    assert!(output.status.success(), "{output:?}");
    let output = git(linked.path(), &["commit", "--allow-empty", "-m", "x"]);
    assert!(!output.status.success());
    assert!(String::from_utf8_lossy(&output.stderr).contains("missing Writer trailer"));
}

#[test]
fn a_new_commit_after_a_merge_still_requires_attribution() {
    let dir = repo();
    for args in [
        vec!["commit", "--allow-empty", "-m", "base\n\nWriter: codex"],
        vec!["checkout", "-b", "other"],
        vec!["commit", "--allow-empty", "-m", "other\n\nWriter: codex"],
        vec!["checkout", "main"],
        vec!["merge", "--no-ff", "other", "-m", "merge without trailers"],
    ] {
        assert!(git(dir.path(), &args).status.success());
    }
    let output = git(
        dir.path(),
        &["commit", "--allow-empty", "-m", "merge without trailers"],
    );
    assert!(!output.status.success());
    let output = git(
        dir.path(),
        &["commit", "--allow-empty", "--amend", "--no-edit"],
    );
    assert!(!output.status.success());
    assert!(String::from_utf8_lossy(&output.stderr).contains("missing Writer trailer"));
}

#[test]
fn automatic_comments_without_scissors_match_the_final_message() {
    let dir = repo();
    assert!(git(dir.path(), &["config", "core.commentChar", "auto"])
        .status
        .success());
    for (message, valid) in [
        ("subject\n\n# body\n\nWriter: codex", true),
        (
            "subject\n\n# leading body\n\nWriter: codex\n\n# retained body",
            false,
        ),
    ] {
        let output = pg_worker_core::git_test_support::command(dir.path())
            .args(["commit", "--allow-empty", "--edit", "-m", message])
            .env("GIT_EDITOR", "true")
            .env("GIT_CONFIG_NOSYSTEM", "1")
            .env("GIT_CONFIG_GLOBAL", "/dev/null")
            .env("GIT_CONFIG_NOSYSTEM", "1")
            .env("GIT_CONFIG_GLOBAL", "/dev/null")
            .env("GIT_AUTHOR_NAME", "Test")
            .env("GIT_AUTHOR_EMAIL", "test@example.invalid")
            .env("GIT_COMMITTER_NAME", "Test")
            .env("GIT_COMMITTER_EMAIL", "test@example.invalid")
            .output()
            .unwrap();
        assert_eq!(output.status.success(), valid, "{output:?}");
    }
}

#[test]
fn attribution_paragraphs_and_misplaced_known_trailers() {
    let dir = repo();
    let message = "fix typo\n\nExplain the correction.\n\nPlan-Item: plangraph:TEST\nHand-Edit: typo correction\nChangelog: skip (typo correction)\n\nCo-Authored-By: Claude <claude@example.invalid>\nClaude-Session: session_test";
    let output = git(dir.path(), &["commit", "--allow-empty", "-m", message]);
    assert!(output.status.success(), "{output:?}");
    for key in ["Plan-Item", "Writer", "Hand-Edit", "Changelog", "Gate-Edit"] {
        let value = match key {
            "Changelog" => "skip (typo correction)",
            "Gate-Edit" => "D-1234",
            _ => "codex",
        };
        for separator in ["\n", "\n\n"] {
            let message =
                format!("subject\n\n{key}: {value}{separator}Prose follows.\n\nWriter: codex");
            let output = git(dir.path(), &["commit", "--allow-empty", "-m", &message]);
            assert!(!output.status.success(), "{message}: {output:?}");
            let stderr = String::from_utf8_lossy(&output.stderr);
            assert!(stderr.contains(&format!("line 3: {key}")), "{stderr}");
            assert!(
                stderr.contains("outside trailing trailer block"),
                "{stderr}"
            );
        }
    }
}

#[test]
fn a_malformed_class_search_line_is_refused_with_the_expected_form() {
    let dir = repo();
    for line in [
        "Class-Search: fixture -> 1 hit, 1 changed (prose after the count)",
        "Class-Search: fixture -> 1 hits, 2 changed",
        "Class-Search: fixture -> some hits",
    ] {
        let message = format!("x\n\n{line}\n\nWriter: codex");
        let output = git(dir.path(), &["commit", "--allow-empty", "-m", &message]);
        assert!(!output.status.success(), "{line}: {output:?}");
        let stderr = String::from_utf8_lossy(&output.stderr);
        assert!(
            stderr.contains("line 3: malformed Class-Search")
                && stderr.contains("expected `Class-Search: <pattern> -> <n> hits, <m> changed`"),
            "{stderr}"
        );
    }
    for line in [
        "Class-Search: fixture -> 1 hit, 1 changed",
        "Class-Search: 0123abcd fixture -> 2 hits, 1 changed",
    ] {
        let message = format!("x\n\n{line}\n\nWriter: codex");
        let output = git(dir.path(), &["commit", "--allow-empty", "-m", &message]);
        assert!(output.status.success(), "{line}: {output:?}");
    }
}

const BAD: &str = "Class-Search: fixture -> 1 hit, 1 changed (prose)";

#[test]
fn a_merge_commit_and_a_stripped_message_keep_the_class_search_check() {
    let dir = repo();
    let root = dir.path();
    let commit = |message: &str| git(root, &["commit", "--allow-empty", "-m", message]);
    assert!(commit("base\n\nWriter: codex").status.success());
    assert!(git(root, &["checkout", "-qb", "side"]).status.success());
    assert!(commit("side\n\nWriter: codex").status.success());
    assert!(git(root, &["checkout", "-q", "main"]).status.success());
    assert!(commit("main\n\nWriter: codex").status.success());
    // The merge exemption covers attribution only.
    let merge = git(
        root,
        &[
            "merge",
            "--no-ff",
            "-m",
            &format!("Merge side\n\n{BAD}"),
            "side",
        ],
    );
    assert!(!merge.status.success(), "{merge:?}");
    let stderr = String::from_utf8_lossy(&merge.stderr);
    assert!(
        stderr.contains("line 3: malformed Class-Search"),
        "{stderr}"
    );
    assert!(git(root, &["merge", "--abort"]).status.success());
    let merge = git(root, &["merge", "--no-ff", "-m", "Merge side", "side"]);
    assert!(merge.status.success(), "{merge:?}");
    // `--cleanup=strip` without an editor keeps a line below a scissors-looking comment.
    let message = format!(
        "x\n\nWriter: codex\n# ------------------------ >8 ------------------------\n{BAD}\n"
    );
    let output = git(
        root,
        &["commit", "--allow-empty", "--cleanup=strip", "-m", &message],
    );
    assert!(!output.status.success(), "{output:?}");
    assert!(
        String::from_utf8_lossy(&output.stderr).contains("malformed Class-Search"),
        "{output:?}"
    );
    assert!(git(root, &["config", "commit.cleanup", "strip"])
        .status
        .success());
    let output = git(root, &["commit", "--allow-empty", "-m", &message]);
    assert!(!output.status.success(), "{output:?}");
}

fn commit_with_editor(root: &Path, args: &[&str], editor: &str) -> Output {
    pg_worker_core::git_test_support::command(root)
        .arg("commit")
        .args(args)
        .env("GIT_EDITOR", editor)
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .env("GIT_AUTHOR_NAME", "Test")
        .env("GIT_AUTHOR_EMAIL", "test@example.invalid")
        .env("GIT_COMMITTER_NAME", "Test")
        .env("GIT_COMMITTER_EMAIL", "test@example.invalid")
        .output()
        .unwrap()
}

fn saved_body(root: &Path) -> String {
    String::from_utf8(git(root, &["log", "-1", "--format=%B"]).stdout).unwrap()
}

#[test]
fn the_check_follows_what_git_keeps_of_an_editor_message() {
    let dir = repo();
    let root = dir.path();
    // Trailing spaces typed in the editor are stripped by Git: the line is valid.
    let editor = "sh -c 'printf \"x\\n\\nClass-Search: fixture -> 1 hit, 1 changed   \\n\\nWriter: codex\\n\" > \"$1\"' --";
    let output = commit_with_editor(root, &["--allow-empty", "--edit", "-m", "m"], editor);
    assert!(output.status.success(), "{output:?}");
    assert!(
        saved_body(root).contains("Class-Search: fixture -> 1 hit, 1 changed\n"),
        "{}",
        saved_body(root)
    );
    // A line typed below the scissors line Git generated is discarded by Git: not checked.
    std::fs::write(root.join("staged.txt"), "x").unwrap();
    assert!(git(root, &["add", "staged.txt"]).status.success());
    let editor = format!("sh -c 'printf \"{BAD}\\n\" >> \"$1\"' --");
    let output = commit_with_editor(
        root,
        &["--edit", "--verbose", "-m", "tail\n\nWriter: codex"],
        &editor,
    );
    assert!(output.status.success(), "{output:?}");
    assert!(
        !saved_body(root).contains("Class-Search"),
        "{}",
        saved_body(root)
    );
    // The same text above the tail is kept by Git and refused.
    std::fs::write(root.join("staged.txt"), "y").unwrap();
    assert!(git(root, &["add", "staged.txt"]).status.success());
    let editor = format!("sh -c 'printf \"x\\n\\n{BAD}\\n\\nWriter: codex\\n\" > \"$1\"' --");
    let output = commit_with_editor(root, &["--edit", "-m", "m"], &editor);
    assert!(!output.status.success(), "{output:?}");
}

#[test]
fn a_copied_scissors_pair_in_a_message_without_an_editor_is_kept_and_checked() {
    let dir = repo();
    let root = dir.path();
    let message = format!(
        "x\n\n# ------------------------ >8 ------------------------\n# Do not modify or remove the line above.\n{BAD}\n\nWriter: codex"
    );
    let output = git(
        root,
        &[
            "commit",
            "--allow-empty",
            "--cleanup=verbatim",
            "-m",
            &message,
        ],
    );
    assert!(!output.status.success(), "{output:?}");
    // Verbatim cleanup keeps trailing whitespace, which the range check refuses.
    assert!(git(root, &["config", "commit.cleanup", "verbatim"])
        .status
        .success());
    let message = "x\n\nClass-Search: fixture -> 1 hit, 1 changed  \n\nWriter: codex";
    let output = git(root, &["commit", "--allow-empty", "-m", message]);
    assert!(!output.status.success(), "{output:?}");
}

fn editor_writes(text: &str) -> String {
    format!("sh -c 'printf \"{text}\" > \"$1\"' --")
}

#[test]
fn the_check_uses_gits_effective_cutoff_and_cleanup_for_an_editor_message() {
    let dir = repo();
    let root = dir.path();
    // A copied marker and hint in an ordinary editor message are comments Git strips, and the line
    // below them stays in the saved message: refused.
    let copied = editor_writes(&format!(
        "x\\n\\n# ------------------------ >8 ------------------------\\n# Do not modify or remove the line above.\\n{BAD}\\n\\nWriter: codex\\n"
    ));
    let output = commit_with_editor(root, &["--allow-empty", "--edit", "-m", "m"], &copied);
    assert!(!output.status.success(), "{output:?}");
    assert!(String::from_utf8_lossy(&output.stderr).contains("malformed Class-Search"));
    // Under --cleanup=scissors the first marker cuts, hint or not: the tail is discarded.
    let cut = editor_writes(&format!(
        "x\\n\\nWriter: codex\\n# ------------------------ >8 ------------------------\\n{BAD}\\n"
    ));
    let output = commit_with_editor(
        root,
        &["--allow-empty", "--edit", "--cleanup=scissors", "-m", "m"],
        &cut,
    );
    assert!(output.status.success(), "{output:?}");
    assert!(
        !saved_body(root).contains("Class-Search"),
        "{}",
        saved_body(root)
    );
}

#[test]
fn the_check_matches_gits_whitespace_and_comment_cleanup() {
    let dir = repo();
    let root = dir.path();
    // --cleanup=verbatim keeps trailing spaces, which the range check refuses.
    let message = "x\n\nClass-Search: fixture -> 1 hit, 1 changed  \n\nWriter: codex";
    let output = git(
        root,
        &[
            "commit",
            "--allow-empty",
            "--cleanup=verbatim",
            "-m",
            message,
        ],
    );
    assert!(!output.status.success(), "{output:?}");
    // A trailing no-break space survives the default editor cleanup: refused.
    let nbsp = editor_writes(
        "x\\n\\nClass-Search: fixture -> 1 hit, 1 changed\\302\\240\\n\\nWriter: codex\\n",
    );
    let output = commit_with_editor(root, &["--allow-empty", "--edit", "-m", "m"], &nbsp);
    assert!(!output.status.success(), "{output:?}");
    // With core.commentChar=C Git strips the line as an editor comment: the commit goes through.
    let removed = editor_writes(&format!("x\\n\\n{BAD}\\n\\nWriter: codex\\n"));
    let output = pg_worker_core::git_test_support::command(root)
        .args([
            "-c",
            "core.commentChar=C",
            "commit",
            "--allow-empty",
            "--edit",
            "-m",
            "m",
        ])
        .env("GIT_EDITOR", removed)
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .env("GIT_AUTHOR_NAME", "Test")
        .env("GIT_AUTHOR_EMAIL", "test@example.invalid")
        .env("GIT_COMMITTER_NAME", "Test")
        .env("GIT_COMMITTER_EMAIL", "test@example.invalid")
        .output()
        .unwrap();
    assert!(output.status.success(), "{output:?}");
    assert!(
        !saved_body(root).contains("Class-Search"),
        "{}",
        saved_body(root)
    );
}
