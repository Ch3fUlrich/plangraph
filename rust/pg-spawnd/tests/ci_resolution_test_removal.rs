//! Exercise the shared commit gate with real Git merge and non-merge commits.
use pg_worker_core::git_test_support;
use std::{
    path::{Path, PathBuf},
    process::{Command, Output},
};
use tempfile::TempDir;

const TEST_PATH: &str = "tests/test_fixture.py";
const BASE_TESTS: &str = "def test_keep():\n    assert True\n";
const WRITER: &str = "Writer: gpt-6.2-sol fixture";
const REMOVAL: &str = "test removal without Test-Removal trailer";

struct Fixture {
    dir: TempDir,
    base: String,
}

impl Fixture {
    fn new() -> Self {
        let dir = tempfile::tempdir().unwrap();
        git(dir.path(), &["init", "-q", "-b", "main"]);
        std::fs::create_dir(dir.path().join("tests")).unwrap();
        std::fs::write(dir.path().join(TEST_PATH), BASE_TESTS).unwrap();
        std::fs::write(dir.path().join("conflict.txt"), "base\n").unwrap();
        git(dir.path(), &["add", "."]);
        git(dir.path(), &["commit", "-qm", "base"]);
        let base = git(dir.path(), &["rev-parse", "HEAD"]);
        Self { dir, base }
    }

    fn git(&self, args: &[&str]) -> String {
        git(self.dir.path(), args)
    }

    fn write(&self, path: &str, text: &str) {
        std::fs::write(self.dir.path().join(path), text).unwrap();
    }

    fn commit(&self, message: &str) -> String {
        self.git(&["add", "-A"]);
        self.git(&["commit", "-qm", message]);
        self.git(&["rev-parse", "HEAD"])
    }

    fn merge(&self, test_file_conflict: bool) {
        self.git(&["checkout", "-qb", "part"]);
        self.write("conflict.txt", "part\n");
        self.write(TEST_PATH, "def test_part():\n    assert True\n");
        self.commit(&format!("part\n\n{WRITER}\nTest-Removal: D-1"));
        self.git(&["checkout", "-q", "main"]);
        self.write("conflict.txt", "main\n");
        if test_file_conflict {
            self.write(TEST_PATH, "def test_keep():\n    assert 1 == 1\n");
        }
        self.commit(&format!("main side\n\n{WRITER}"));
        let out = git_test_support::command(self.dir.path())
            .args(["merge", "--no-commit", "--no-ff", "part"])
            .output()
            .unwrap();
        assert_eq!(out.status.code(), Some(1), "{out:?}");
        self.git(&["rev-parse", "--verify", "MERGE_HEAD"]);
        self.write("conflict.txt", "resolved\n");
    }

    fn gate(&self, head: &str) -> Output {
        let gate = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tools/ci_commit_gate.py");
        let mut command = Command::new(if cfg!(windows) { "python" } else { "python3" });
        git_test_support::configure(&mut command, self.dir.path());
        command
            .arg(gate)
            .args(["check", "--base", &self.base, "--head", head])
            .output()
            .unwrap()
    }

    fn assert_removal_refused(&self, commit: &str, head: &str) {
        let out = self.gate(head);
        assert_eq!(out.status.code(), Some(1), "{out:?}");
        assert_eq!(
            String::from_utf8(out.stdout).unwrap().trim(),
            format!("{commit}: {REMOVAL}")
        );
    }
}

fn git(dir: &Path, args: &[&str]) -> String {
    let out = git_test_support::command(dir).args(args).output().unwrap();
    assert!(out.status.success(), "{args:?} {out:?}");
    String::from_utf8(out.stdout).unwrap().trim().into()
}

#[test]
fn resolution_deleting_test_requires_its_own_trailer() {
    for authorized in [false, true] {
        let fx = Fixture::new();
        fx.merge(false);
        std::fs::remove_file(fx.dir.path().join(TEST_PATH)).unwrap();
        let trailer = if authorized {
            "\n\nTest-Removal: D-2"
        } else {
            ""
        };
        let head = fx.commit(&format!("resolution{trailer}"));
        if authorized {
            let out = fx.gate(&head);
            assert!(out.status.success(), "{out:?}");
        } else {
            fx.assert_removal_refused(&head, &head);
            fx.write(TEST_PATH, "def test_part():\n    assert True\n");
            let restored = fx.commit(&format!("restore on a later commit\n\n{WRITER}"));
            fx.assert_removal_refused(&head, &restored);
        }
    }
}

#[test]
fn resolution_keeping_first_parent_can_drop_a_second_parent_test() {
    let fx = Fixture::new();
    fx.merge(true);
    fx.git(&["checkout", "--ours", TEST_PATH]);
    let head = fx.commit("resolution keeping first parent");
    fx.assert_removal_refused(&head, &head);
}

#[test]
fn clean_resolution_can_edit_a_test_and_preserve_an_authorized_part_deletion() {
    let fx = Fixture::new();
    fx.merge(false);
    fx.write(TEST_PATH, "def test_part():\n    assert 1 == 1\n");
    let head = fx.commit("resolution preserving the merged test");
    let out = fx.gate(&head);
    assert!(out.status.success(), "{out:?}");
}

#[test]
fn non_merge_test_removal_keeps_the_same_trailer_requirement() {
    let fx = Fixture::new();
    std::fs::remove_file(fx.dir.path().join(TEST_PATH)).unwrap();
    let head = fx.commit(&format!("remove test\n\n{WRITER}"));
    fx.assert_removal_refused(&head, &head);
    fx.git(&[
        "commit",
        "--amend",
        "-qm",
        &format!("remove test\n\n{WRITER}\nTest-Removal: D-1"),
    ]);
    let authorized = fx.git(&["rev-parse", "HEAD"]);
    let out = fx.gate(&authorized);
    assert!(out.status.success(), "{out:?}");
}
