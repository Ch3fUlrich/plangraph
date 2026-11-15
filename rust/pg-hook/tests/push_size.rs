#![cfg(unix)]
mod common;
use std::{
    fs,
    io::Write,
    path::Path,
    process::{Command, Output, Stdio},
};

fn isolated(command: &mut Command, root: &Path) {
    for (key, _) in std::env::vars_os() {
        if key.to_string_lossy().starts_with("GIT_") {
            command.env_remove(key);
        }
    }
    command
        .current_dir(root)
        .env("HOME", root)
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_AUTHOR_NAME", "Test")
        .env("GIT_COMMITTER_NAME", "Test")
        .env("GIT_AUTHOR_EMAIL", "t@example.invalid")
        .env("GIT_COMMITTER_EMAIL", "t@example.invalid");
}
fn git(root: &Path, args: &[&str]) -> String {
    let mut cmd = Command::new("git");
    isolated(&mut cmd, root);
    let out = cmd.args(args).output().unwrap();
    assert!(out.status.success(), "{out:?}");
    String::from_utf8(out.stdout).unwrap().trim().into()
}
fn commit(root: &Path) -> String {
    git(root, &["add", "."]);
    git(
        root,
        &[
            "commit",
            "-qm",
            "fixture\n\nPlan-Item: plangraph:TEST\nWriter: codex/test\nChangelog: skip (test fixture)",
        ],
    );
    git(root, &["rev-parse", "HEAD"])
}
fn push(root: &Path, head: &str, base: &str) -> Output {
    let source = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let mut cmd = common::python(&source.join("tools/hooks/pre-push"));
    isolated(&mut cmd, root);
    // Exercise preflight's second size check too, using the separate tools.
    cmd.env_remove("PLANGRAPH_SCRUB_WORDS")
        .env_remove("PLANGRAPH_SCRUB_RULES")
        .env_remove("PREFLIGHT_SKIP")
        .env_remove("FILESIZE_REQUIRE_BASE")
        .env("PREFLIGHT_FAST", "1")
        .env("PREFLIGHT_TOOLS_DIR", source.join("tools"))
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut child = cmd.spawn().unwrap();
    writeln!(
        child.stdin.take().unwrap(),
        "refs/heads/lane {head} refs/heads/lane {base}"
    )
    .unwrap();
    child.wait_with_output().unwrap()
}

#[test]
fn push_tool_and_baseline_change_passes_but_oversize_and_scrub_fail() {
    let tmp = tempfile::tempdir().unwrap();
    let root = tmp.path();
    git(root, &["init", "-q", "-b", "main"]);
    fs::create_dir(root.join("tools")).unwrap();
    fs::write(root.join("tools/new_code_allowlist.txt"), "").unwrap();
    let source = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    fs::copy(
        source.join("tools/preflight.py"),
        root.join("tools/preflight.py"),
    )
    .unwrap();
    fs::write(root.join("legacy.md"), "legacy\n".repeat(900)).unwrap();
    fs::write(root.join(".filesize-baseline"), "legacy.md 900\n").unwrap();
    fs::write(root.join("tools/check_file_size.py"), "# old checker\n").unwrap();
    let base = commit(root);
    git(root, &["update-ref", "refs/remotes/origin/main", &base]);
    // A changed tool and pruned baseline must not rejudge unchanged legacy
    // files using the hooks checker plus the candidate's incompatible data.
    fs::write(
        root.join("tools/check_file_size.py"),
        "raise SystemExit(0)\n",
    )
    .unwrap();
    fs::write(root.join(".filesize-baseline"), "").unwrap();
    let head = commit(root);
    let out = push(root, &head, &base);
    assert!(out.status.success(), "{out:?}");
    let out = push(root, &head, &"0".repeat(40));
    assert!(out.status.success(), "{out:?}");
    // The checker and data are selected by pushed SHA, never dirty disk/HEAD.
    fs::write(root.join(".filesize-baseline"), "malformed\n").unwrap();
    let out = push(root, &head, &base);
    assert!(out.status.success(), "{out:?}");
    fs::write(root.join(".filesize-baseline"), "").unwrap();
    fs::write(root.join("large.rs"), "// line\n".repeat(801)).unwrap();
    fs::write(
        root.join(".filesize-allow"),
        "large.rs candidate exemption\n",
    )
    .unwrap();
    let large = commit(root);
    fs::write(root.join("large.rs"), "// dirty short file\n").unwrap();
    let out = push(root, &large, &base);
    assert!(!out.status.success(), "{out:?}");
    assert!(
        String::from_utf8_lossy(&out.stderr).contains("large.rs"),
        "{out:?}"
    );
    // Pushing the earlier clean commit must pass even while HEAD is oversized.
    let out = push(root, &head, &base);
    assert!(out.status.success(), "{out:?}");
    git(root, &["reset", "--hard", &head]);
    fs::write(root.join("legacy.md"), "legacy\n".repeat(901)).unwrap();
    let grown = commit(root);
    let out = push(root, &grown, &base);
    assert!(!out.status.success(), "{out:?}");
    assert!(
        String::from_utf8_lossy(&out.stderr).contains("baseline 900"),
        "{out:?}"
    );
    git(root, &["reset", "--hard", &head]);
    let ip = [193, 2, 2, 1].map(|n| n.to_string()).join(".");
    fs::write(root.join("leak.txt"), format!("endpoint={ip}\n")).unwrap();
    let leak = commit(root);
    let out = push(root, &leak, &base);
    assert!(!out.status.success(), "{out:?}");
    assert!(
        String::from_utf8_lossy(&out.stderr).contains("scrub hit"),
        "{out:?}"
    );
}
