use super::build_tests::g;
use super::cut::*;
use std::path::{Path, PathBuf};
use std::process::Command;

pub(super) const TRAILERS: &str = "Plan-Item: plangraph:X\nWriter: claude-sonnet-5-5 session_abc\nHarness: claude\nChangelog: skip (test)";

pub(super) fn gate() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tools/ci_commit_gate.py")
}

pub(super) fn python() -> Command {
    crate::base_fresh::python_command(None)
}

pub(super) struct Fx {
    pub(super) dir: tempfile::TempDir,
    pub(super) base: String,
}

impl Fx {
    pub(super) fn new() -> Fx {
        let dir = tempfile::tempdir().unwrap();
        g(dir.path(), &["init", "-q", "-b", "main"]);
        g(dir.path(), &["commit", "-q", "--allow-empty", "-m", "base"]);
        let base = g(dir.path(), &["rev-parse", "HEAD"]);
        Fx { dir, base }
    }
    pub(super) fn p(&self) -> &Path {
        self.dir.path()
    }
    /// A source commit with the writer trailer block; `file` may be a gate file.
    pub(super) fn source(&self, file: &str, trailers: &str) -> String {
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
    pub(super) fn cut(&self, commits: &[String]) -> Result<Vec<String>, String> {
        self.cut_to("slice/a", commits, &gate(), &python)
    }
    pub(super) fn cut_to(
        &self,
        branch: &str,
        commits: &[String],
        gate: &Path,
        python: &dyn Fn() -> Command,
    ) -> Result<Vec<String>, String> {
        cut(
            &Cut {
                repo: self.p(),
                base: &self.base,
                branch,
                commits,
                gate,
            },
            python,
        )
    }
    pub(super) fn message(&self, rev: &str) -> String {
        g(self.p(), &["log", "-1", "--format=%B", rev])
    }
    pub(super) fn worktrees(&self) -> usize {
        g(self.p(), &["worktree", "list", "--porcelain"])
            .matches("worktree ")
            .count()
    }
}

#[test]
fn cut_commits_pass_the_gate_with_picked_from_inside_the_trailer_block() {
    let fx = Fx::new();
    let a = fx.source("a.txt", TRAILERS);
    let b = fx.source("b.txt", TRAILERS);
    fx.cut(&[a.clone(), b.clone()]).unwrap();
    let head = g(fx.p(), &["rev-parse", "slice/a"]);
    let last = fx.message(&head);
    let block = last.rsplit("\n\n").next().unwrap();
    for t in [
        "Plan-Item: plangraph:X".to_string(),
        format!("Picked-From: {b}"),
    ] {
        assert!(block.lines().any(|l| l == t), "{t} not in block:\n{last}");
    }
    assert!(!last.contains("cherry picked") && !last.contains("Gate-Edit"));
    assert!(fx
        .message(&format!("{head}^"))
        .contains(&format!("Picked-From: {a}")));
    check(fx.p(), &fx.base, &head, &gate(), &python).unwrap();
}

#[test]
fn a_gate_file_commit_is_refused_before_any_pick_and_no_branch_is_left() {
    let fx = Fx::new();
    let flow = fx.source(".github/workflows/w.yml", TRAILERS);
    let err = fx.cut(std::slice::from_ref(&flow)).unwrap_err();
    // Named by the source id: refused before the pick, not by the check of the picked commit.
    assert!(
        err.contains("grants are not accepted yet") && err.contains(&flow),
        "{err}"
    );
    assert!(err.contains(".github/workflows/w.yml"), "{err}");
    assert!(g(fx.p(), &["branch", "--list", "slice/a"]).is_empty());
    assert_eq!(fx.worktrees(), 1);
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
    fx.cut(std::slice::from_ref(&hand)).unwrap();
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
    assert!(fx.cut(&[merge]).unwrap_err().contains("single-parent"));
}

/// `cargo test` runs in the crate directory, so this path is relative to the caller, not to the worktree.
#[test]
fn a_gate_path_relative_to_the_caller_still_works_in_the_cut_checkout() {
    let fx = Fx::new();
    let src = fx.source("a.txt", TRAILERS);
    let relative = Path::new("../../tools/ci_commit_gate.py");
    assert!(relative.is_file(), "the test runs in the crate directory");
    fx.cut_to("slice/rel", &[src], relative, &python).unwrap();
    let head = g(fx.p(), &["rev-parse", "slice/rel"]);
    check(fx.p(), &fx.base, &head, relative, &python).unwrap();
}

#[test]
fn a_missing_gate_names_the_reason() {
    let fx = Fx::new();
    let src = fx.source("a.txt", TRAILERS);
    let err = fx
        .cut_to(
            "slice/missing",
            &[src],
            Path::new("no/such/gate.py"),
            &python,
        )
        .unwrap_err();
    assert!(err.contains("no/such/gate.py"), "{err}");
}

#[test]
fn a_branch_created_while_the_gate_runs_is_not_overwritten() {
    let fx = Fx::new();
    let src = fx.source("a.txt", TRAILERS);
    let rival = fx.source("r.txt", TRAILERS);
    let creating = || {
        // The gate of the cut runs this: another job creates the branch in the meantime.
        g(fx.p(), &["branch", "-f", "slice/a", &rival]);
        python()
    };
    let err = fx
        .cut_to("slice/a", &[src], &gate(), &creating)
        .unwrap_err();
    assert!(err.contains("already exists"), "{err}");
    assert_eq!(g(fx.p(), &["rev-parse", "slice/a"]), rival);
}
