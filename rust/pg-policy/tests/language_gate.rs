use std::{
    fs,
    path::Path,
    process::{Command, Output},
};
fn git(root: &Path, args: &[&str]) -> String {
    let output = Command::new("git")
        .current_dir(root)
        .args(args)
        .env("GIT_AUTHOR_NAME", "Test")
        .env("GIT_COMMITTER_NAME", "Test")
        .env("GIT_AUTHOR_EMAIL", "test@example.invalid")
        .env("GIT_COMMITTER_EMAIL", "test@example.invalid")
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8(output.stdout).unwrap().trim().to_owned()
}
fn check(reason: Option<&str>, remove: bool, forged: bool) -> Output {
    let tmp = tempfile::tempdir().unwrap();
    let root = tmp.path();
    git(root, &["init", "-q"]);
    fs::create_dir(root.join("tools")).unwrap();
    fs::write(
        root.join("tools/new_code_allowlist.txt"),
        "old.py | historical\n",
    )
    .unwrap();
    fs::write(
        root.join("tools/new_code_decisions_index.txt"),
        "D-5912 | OPERATOR\nD-5840 | OPERATOR\nD-5830 | OPERATOR\nD-5999 | ROUTER\n",
    )
    .unwrap();
    git(root, &["add", "."]);
    git(root, &["commit", "-qm", "base"]);
    let base = git(root, &["rev-parse", "HEAD"]);
    let entry = reason
        .map(|r| format!("new.py | {r}\n"))
        .unwrap_or_default();
    fs::write(
        root.join("tools/new_code_allowlist.txt"),
        if remove {
            entry
        } else {
            format!("old.py | historical\n{entry}")
        },
    )
    .unwrap();
    if forged {
        fs::write(
            root.join("tools/new_code_decisions_index.txt"),
            "D-5999 | OPERATOR\n",
        )
        .unwrap();
    }
    if reason.is_some() {
        fs::write(root.join("new.py"), "print('new')\n").unwrap();
    }
    git(root, &["add", "."]);
    git(root, &["commit", "-qm", "change"]);
    Command::new(env!("CARGO_BIN_EXE_pg-policy"))
        .current_dir(root)
        .args(["--base", &base])
        .output()
        .unwrap()
}
#[test]
fn added_allowlist_requires_base_operator_authority() {
    for reason in [
        "D-5840",
        "OPERATOR D-5830",
        "OPERATOR D-5999",
        "no decision",
        "D-59120",
    ] {
        let result = check(Some(reason), false, false);
        assert!(!result.status.success(), "{reason}");
    }
    assert!(check(Some("OPERATOR D-5912"), false, false)
        .status
        .success());
    assert!(check(None, true, false).status.success());
    assert!(!check(Some("OPERATOR D-5999"), false, true).status.success());
}
