use super::build_tests::g;
use super::cut::*;
use super::go_verify::Grant;
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
        let granted: Vec<String> = edits.iter().map(|(_, d)| d.to_string()).collect();
        self.cut_granted(commits, edits, &granted)
    }
    fn cut_granted(
        &self,
        commits: &[String],
        edits: &[(&str, &str)],
        grants: &[String],
    ) -> Result<Vec<String>, String> {
        let gate_edits: BTreeMap<String, String> = edits
            .iter()
            .map(|(a, b)| (a.to_string(), b.to_string()))
            .collect();
        // Every D-id is granted for every named source head and the workflow directory.
        let grants: Vec<Grant> = grants
            .iter()
            .flat_map(|d| {
                commits.iter().map(move |h| Grant {
                    d_id: d.clone(),
                    prefix: ".github/workflows/".into(),
                    head: h.clone(),
                })
            })
            .collect();
        cut(
            &Cut {
                repo: self.p(),
                base: &self.base,
                branch: "slice/a",
                commits,
                gate_edits: &gate_edits,
                grants: &grants,
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
    let grants = [Grant {
        d_id: "D-9999".into(),
        prefix: ".github/workflows/".into(),
        head: flow,
    }];
    check(fx.p(), &fx.base, &head, &gate(), &grants, &python).unwrap();
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
    let err = check(fx.p(), &fx.base, &head, &gate(), &[], &python).unwrap_err();
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
            grants: &[],
            gate: relative,
        },
        &python,
    )
    .unwrap();
    let head = g(fx.p(), &["rev-parse", "slice/rel"]);
    check(fx.p(), &fx.base, &head, relative, &[], &python).unwrap();
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
            grants: &[],
            gate: Path::new("no/such/gate.py"),
        },
        &python,
    )
    .unwrap_err();
    assert!(err.contains("no/such/gate.py"), "{err}");
}

#[test]
fn a_gate_edit_d_id_the_go_row_does_not_grant_is_refused_before_any_pick() {
    let fx = Fx::new();
    let flow = fx.source(".github/workflows/w.yml", TRAILERS);
    let err = fx
        .cut_granted(
            std::slice::from_ref(&flow),
            &[(&flow, "D-1")],
            &["D-2".to_string()],
        )
        .unwrap_err();
    assert!(err.contains("D-1") && err.contains("not granted"), "{err}");
    assert!(g(fx.p(), &["branch", "--list", "slice/a"]).is_empty());
}

#[test]
fn a_gate_edit_for_a_commit_that_is_not_cut_is_refused() {
    let fx = Fx::new();
    let plain = fx.source("a.txt", TRAILERS);
    let other = fx.source("b.txt", TRAILERS);
    let err = fx.cut(&[plain], &[(&other, "D-5")]).unwrap_err();
    assert!(err.contains("not cut"), "{err}");
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
    let none = BTreeMap::new();
    let err = cut(
        &Cut {
            repo: fx.p(),
            base: &fx.base,
            branch: "slice/a",
            commits: &[src],
            gate_edits: &none,
            grants: &[],
            gate: &gate(),
        },
        &creating,
    )
    .unwrap_err();
    assert!(err.contains("already exists"), "{err}");
    assert_eq!(g(fx.p(), &["rev-parse", "slice/a"]), rival);
}

#[test]
fn a_gate_edit_the_source_already_carries_must_be_granted_too() {
    let fx = Fx::new();
    let flow = fx.source(
        ".github/workflows/w.yml",
        &format!("{TRAILERS}\nGate-Edit: D-77"),
    );
    let err = fx
        .cut_granted(std::slice::from_ref(&flow), &[], &["D-1".to_string()])
        .unwrap_err();
    assert!(
        err.contains("D-77") && err.contains("does not grant"),
        "{err}"
    );
    assert!(g(fx.p(), &["branch", "--list", "slice/a"]).is_empty());
    fx.cut_granted(&[flow], &[], &["D-77".to_string()]).unwrap();
}

#[test]
fn a_gate_edit_git_does_not_parse_in_the_source_but_the_gate_sees_after_cutting_is_refused() {
    let fx = Fx::new();
    // The `(cherry picked ...)` paragraph is the last one, so git finds no Gate-Edit trailer in the source;
    // cutting drops it and the paragraph before becomes the trailer block the commit gate reads.
    let src = fx.source(
        ".github/workflows/w.yml",
        &format!("{TRAILERS}\nGate-Edit: D-88\n\n(cherry picked from commit ab12)"),
    );
    let parsed = g(
        fx.p(),
        &[
            "log",
            "-1",
            "--format=%(trailers:key=Gate-Edit,valueonly)",
            &src,
        ],
    );
    assert!(
        parsed.is_empty(),
        "the fixture must hide it from git: {parsed}"
    );
    let err = fx
        .cut_granted(std::slice::from_ref(&src), &[], &["D-1".to_string()])
        .unwrap_err();
    assert!(
        err.contains("D-88") && err.contains("does not grant"),
        "{err}"
    );
    assert!(g(fx.p(), &["branch", "--list", "slice/a"]).is_empty());
    fx.cut_granted(&[src], &[], &["D-88".to_string()]).unwrap();
}

/// A repository whose base commit holds `gate_text` as `tools/ci_commit_gate.py`.
fn fx_with_base_gate(gate_text: &str) -> Fx {
    let fx = Fx::new();
    std::fs::create_dir_all(fx.p().join("tools")).unwrap();
    std::fs::write(fx.p().join("tools/ci_commit_gate.py"), gate_text).unwrap();
    g(fx.p(), &["add", "-A"]);
    g(fx.p(), &["commit", "-q", "-m", "base with gate"]);
    let base = g(fx.p(), &["rev-parse", "HEAD"]);
    Fx { base, ..fx }
}

fn args(list: &[&str]) -> Vec<String> {
    list.iter().map(|a| a.to_string()).collect()
}

#[test]
fn cut_uses_the_base_gate() {
    let fx = fx_with_base_gate("import sys\nprint('BASE-GATE', *sys.argv[1:])\nsys.exit(1)\n");
    let src = fx.source("a.txt", TRAILERS);
    let base = fx.base.clone();
    // The working copy holds a passing gate.
    std::fs::write(
        fx.p().join("tools/ci_commit_gate.py"),
        "import sys\nsys.exit(0)\n",
    )
    .unwrap();
    let go = |extra: &[&str]| {
        let mut a = vec![
            "cut",
            "--repo",
            fx.p().to_str().unwrap(),
            "--base",
            "HEAD",
            "--branch",
            "slice/a",
            "--commits",
            &src[..12],
        ];
        a.extend_from_slice(extra);
        super::cli::run_inner(fx.p(), &args(&a), &python)
    };
    // The gate of the base commit refuses.
    let err = go(&[]).unwrap_err();
    assert!(err.contains("BASE-GATE"), "{err}");
    // It is called with full commit ids, resolved once.
    assert!(err.contains(&base), "{err}");
    assert!(g(fx.p(), &["branch", "--list", "slice/a"]).is_empty());
    // No flag names another gate.
    let err = go(&["--gate", "tools/ci_commit_gate.py"]).unwrap_err();
    assert!(err.contains("--gate is not accepted"), "{err}");
    // An abbreviated input is recorded as the full id.
    let ok = fx_with_base_gate("import sys\nsys.exit(0)\n");
    let src = ok.source("a.txt", TRAILERS);
    let a = args(&[
        "cut",
        "--repo",
        ok.p().to_str().unwrap(),
        "--base",
        "HEAD",
        "--branch",
        "slice/a",
        "--commits",
        &src[..12],
    ]);
    super::cli::run_inner(ok.p(), &a, &python).unwrap();
    assert!(ok
        .message("slice/a")
        .contains(&format!("Picked-From: {src}")));
}

#[test]
fn cut_check_refuses_an_ungranted_gate_edit() {
    let fx = fx_with_base_gate(&std::fs::read_to_string(gate()).unwrap());
    let flow = fx.source(
        ".github/workflows/w.yml",
        &format!("{TRAILERS}\nGate-Edit: D-5"),
    );
    // A hand-cut branch: a well-formed Gate-Edit, so the gate accepts it, but the record grants nothing for it.
    g(fx.p(), &["checkout", "-q", "-b", "hand", &fx.base]);
    g(fx.p(), &["cherry-pick", &flow]);
    let msg = format!("{}\n\nPicked-From: {flow}", fx.message("HEAD"));
    g(fx.p(), &["commit", "-q", "--amend", "-m", &msg]);
    g(fx.p(), &["checkout", "-q", "main"]);
    let write = |name: &str, rows: &[(&str, &str)]| {
        let path = fx.p().join(name);
        let grants: Vec<Grant> = rows
            .iter()
            .map(|(d, head)| Grant {
                d_id: d.to_string(),
                prefix: ".github/workflows/".into(),
                head: head.to_string(),
            })
            .collect();
        std::fs::write(&path, serde_json::to_vec(&grants).unwrap()).unwrap();
        path
    };
    let other = write("other.json", &[("D-6", &flow)]);
    let wrong_head = write("head.json", &[("D-5", &fx.base)]);
    let right = write("right.json", &[("D-5", &flow)]);
    let run = |grants: Option<&std::path::Path>| {
        let mut a = vec![
            "cut-check",
            "--repo",
            fx.p().to_str().unwrap(),
            "--base",
            &fx.base,
            "--head",
            "hand",
        ];
        if let Some(f) = grants {
            a.extend(["--grants", f.to_str().unwrap()]);
        }
        super::cli::run_inner(fx.p(), &args(&a), &python)
    };
    for bad in [None, Some(other.as_path()), Some(wrong_head.as_path())] {
        let err = run(bad).unwrap_err();
        assert!(
            err.contains("D-5") && err.contains("does not grant"),
            "{err}"
        );
    }
    run(Some(&right)).unwrap();
}

#[test]
fn a_grant_for_another_path_does_not_cover_the_gate_file() {
    let fx = Fx::new();
    let flow = fx.source(".github/workflows/w.yml", TRAILERS);
    let grants = [Grant {
        d_id: "D-3".into(),
        prefix: "docs/".into(),
        head: flow.clone(),
    }];
    let edits = BTreeMap::from([(flow.clone(), "D-3".to_string())]);
    let err = cut(
        &Cut {
            repo: fx.p(),
            base: &fx.base,
            branch: "slice/a",
            commits: std::slice::from_ref(&flow),
            gate_edits: &edits,
            grants: &grants,
            gate: &gate(),
        },
        &python,
    )
    .unwrap_err();
    assert!(
        err.contains(".github/workflows/w.yml") && err.contains("no grant covers"),
        "{err}"
    );
    assert!(g(fx.p(), &["branch", "--list", "slice/a"]).is_empty());
}

#[test]
fn a_finished_cut_leaves_no_scratch_worktree() {
    let fx = Fx::new();
    let src = fx.source("a.txt", TRAILERS);
    let lines = fx.cut(&[src], &[]).unwrap();
    assert_eq!(lines.len(), 1, "{lines:?}");
    // The scratch worktree is gone from the registry and the disk.
    let list = g(fx.p(), &["worktree", "list", "--porcelain"]);
    assert_eq!(list.matches("worktree ").count(), 1, "{list}");
}
