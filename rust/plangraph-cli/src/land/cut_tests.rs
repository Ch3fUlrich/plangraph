use super::build_tests::g;
use super::cut::*;
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::process::Command;

const TRAILERS: &str = "Plan-Item: plangraph:X\nWriter: claude-sonnet-5-5 session_abc\nHarness: claude\nChangelog: skip (test)";

fn gate() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tools/ci_commit_gate.py")
}

fn python() -> Command {
    crate::base_fresh::python_command(None)
}

struct Fx {
    dir: tempfile::TempDir,
    base: String,
}

impl Fx {
    fn new() -> Fx {
        let dir = tempfile::tempdir().unwrap();
        g(dir.path(), &["init", "-q", "-b", "main"]);
        g(dir.path(), &["commit", "-q", "--allow-empty", "-m", "base"]);
        let base = g(dir.path(), &["rev-parse", "HEAD"]);
        Fx { dir, base }
    }
    fn p(&self) -> &Path {
        self.dir.path()
    }
    /// A source commit with the writer trailer block; `file` may be a gate file.
    fn source(&self, file: &str, trailers: &str) -> String {
        g(self.p(), &["checkout", "-q", "--detach", &self.base]);
        let path = self.p().join(file);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, "x\n").unwrap();
        g(self.p(), &["add", "-A"]);
        g(
            self.p(),
            &["commit", "-q", "-m", &format!("edit {file}\n\n{trailers}")],
        );
        let sha = g(self.p(), &["rev-parse", "HEAD"]);
        g(self.p(), &["checkout", "-q", "main"]);
        sha
    }
    fn cut(&self, commits: &[String], edits: &[(&str, &str)]) -> Result<Vec<String>, String> {
        let gate_edits: BTreeMap<String, String> = edits
            .iter()
            .map(|(a, b)| (a.to_string(), b.to_string()))
            .collect();
        cut(
            &Cut {
                repo: self.p(),
                base: &self.base,
                branch: "slice/a",
                commits,
                gate_edits: &gate_edits,
                gate: &gate(),
            },
            &python,
        )
    }
    fn message(&self, rev: &str) -> String {
        g(self.p(), &["log", "-1", "--format=%B", rev])
    }
}

#[test]
fn cut_commits_pass_the_gate_with_gate_edit_inside_the_trailer_block() {
    let fx = Fx::new();
    let plain = fx.source("a.txt", TRAILERS);
    let flow = fx.source(".github/workflows/w.yml", TRAILERS);
    fx.cut(&[plain.clone(), flow.clone()], &[(&flow, "D-9999")])
        .unwrap();
    let head = g(fx.p(), &["rev-parse", "slice/a"]);
    let last = fx.message(&head);
    let block = last.rsplit("\n\n").next().unwrap();
    for t in [
        "Plan-Item: plangraph:X".to_string(),
        "Gate-Edit: D-9999".to_string(),
        format!("Picked-From: {flow}"),
    ] {
        assert!(block.lines().any(|l| l == t), "{t} not in block:\n{last}");
    }
    assert!(!last.contains("cherry picked"));
    let first = fx.message(&format!("{head}^"));
    assert!(first.contains(&format!("Picked-From: {plain}")));
    assert!(!first.contains("Gate-Edit"));
    cut_check(&fx, &head);
}

fn cut_check(fx: &Fx, head: &str) {
    check(fx.p(), &fx.base, head, &gate(), &python).unwrap();
}

#[test]
fn a_gate_file_without_a_granted_gate_edit_is_refused_and_no_branch_is_left() {
    let fx = Fx::new();
    let flow = fx.source(".github/workflows/w.yml", TRAILERS);
    let err = fx.cut(&[flow], &[]).unwrap_err();
    assert!(err.contains("gate edit without Gate-Edit"), "{err}");
    assert!(g(fx.p(), &["branch", "--list", "slice/a"]).is_empty());
    assert_eq!(
        g(fx.p(), &["worktree", "list", "--porcelain"])
            .matches("worktree ")
            .count(),
        1
    );
}

#[test]
fn a_source_with_a_cherry_pick_line_is_cut_without_it() {
    let fx = Fx::new();
    let src = fx.source("a.txt", TRAILERS);
    // A hand pick with -x, then cut from that commit.
    g(fx.p(), &["checkout", "-q", "--detach", &fx.base]);
    g(fx.p(), &["cherry-pick", "-x", &src]);
    let hand = g(fx.p(), &["rev-parse", "HEAD"]);
    g(fx.p(), &["checkout", "-q", "main"]);
    assert!(fx.message(&hand).contains("(cherry picked from commit"));
    fx.cut(std::slice::from_ref(&hand), &[]).unwrap();
    let cut = fx.message("slice/a");
    assert!(!cut.contains("cherry picked"), "{cut}");
    assert!(cut.contains(&format!("Picked-From: {hand}")));
}

#[test]
fn a_hand_cherry_pick_x_branch_is_refused_by_the_check() {
    let fx = Fx::new();
    let src = fx.source("a.txt", TRAILERS);
    g(fx.p(), &["checkout", "-q", "-b", "hand", &fx.base]);
    g(fx.p(), &["cherry-pick", "-x", &src]);
    let head = g(fx.p(), &["rev-parse", "HEAD"]);
    let err = check(fx.p(), &fx.base, &head, &gate(), &python).unwrap_err();
    assert!(err.contains("cherry picked"), "{err}");
}

#[test]
fn a_merge_commit_source_is_refused() {
    let fx = Fx::new();
    let a = fx.source("a.txt", TRAILERS);
    g(fx.p(), &["checkout", "-q", "--detach", &fx.base]);
    g(fx.p(), &["merge", "-q", "--no-ff", "-m", "merge", &a]);
    let merge = g(fx.p(), &["rev-parse", "HEAD"]);
    g(fx.p(), &["checkout", "-q", "main"]);
    assert!(fx.cut(&[merge], &[]).unwrap_err().contains("single-parent"));
}

/// `cargo test` runs in the crate directory, so this path is relative to the caller, not to the worktree.
#[test]
fn a_gate_path_relative_to_the_caller_still_works_in_the_cut_worktree() {
    let fx = Fx::new();
    let src = fx.source("a.txt", TRAILERS);
    let relative = Path::new("../../tools/ci_commit_gate.py");
    assert!(relative.is_file(), "the test runs in the crate directory");
    let none = BTreeMap::new();
    cut(
        &Cut {
            repo: fx.p(),
            base: &fx.base,
            branch: "slice/rel",
            commits: &[src],
            gate_edits: &none,
            gate: relative,
        },
        &python,
    )
    .unwrap();
    let head = g(fx.p(), &["rev-parse", "slice/rel"]);
    check(fx.p(), &fx.base, &head, relative, &python).unwrap();
}

#[test]
fn a_missing_gate_names_the_reason() {
    let fx = Fx::new();
    let src = fx.source("a.txt", TRAILERS);
    let none = BTreeMap::new();
    let err = cut(
        &Cut {
            repo: fx.p(),
            base: &fx.base,
            branch: "slice/missing",
            commits: &[src],
            gate_edits: &none,
            gate: Path::new("no/such/gate.py"),
        },
        &python,
    )
    .unwrap_err();
    assert!(err.contains("no/such/gate.py"), "{err}");
}
