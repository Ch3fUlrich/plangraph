//! `land-train cut-check`: a range of a hand-cut branch is refused for a cherry-pick marker, a `Gate-Edit:` line or
//! a protected path before the pinned gate runs, and the check leaves the caller's repository alone.
use super::build_tests::g;
use super::cut_gate::{scan_commit, PinnedGate};
use std::path::{Path, PathBuf};
use std::process::Command;

pub(super) const TRAILERS: &str = "Plan-Item: plangraph:X\nWriter: claude-sonnet-5-5 session_abc\nHarness: claude\nChangelog: skip (test)";

pub(super) fn gate() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tools/ci_commit_gate.py")
}

pub(super) fn python() -> Command {
    crate::base_fresh::python_command(None)
}

/// A repository on `main` with one base commit.
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
    /// A commit on `main` that writes `file`; it becomes the base of the fixture.
    fn commit_file(&mut self, file: &str, content: &str, message: &str) {
        let path = self.p().join(file);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, content).unwrap();
        g(self.p(), &["add", "-A"]);
        g(self.p(), &["commit", "-q", "-m", message]);
        self.base = g(self.p(), &["rev-parse", "HEAD"]);
    }
    /// A commit on a detached head over `parent` that writes `file`, with the writer trailer block; `main` is
    /// checked out again afterwards.
    pub(super) fn child(&self, parent: &str, file: &str, content: &str, extra: &str) -> String {
        g(self.p(), &["checkout", "-q", "--detach", parent]);
        let path = self.p().join(file);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, content).unwrap();
        g(self.p(), &["add", "-A"]);
        g(
            self.p(),
            &[
                "commit",
                "-q",
                "-m",
                &format!("edit {file}\n\n{TRAILERS}\n{extra}"),
            ],
        );
        let sha = g(self.p(), &["rev-parse", "HEAD"]);
        g(self.p(), &["checkout", "-q", "main"]);
        sha
    }
    /// A one-commit hand-cut range over the base: `file` is written and `extra` ends the trailer block.
    pub(super) fn hand_cut(&self, file: &str, extra: &str) -> String {
        self.child(&self.base, file, "x\n", extra)
    }
}

/// A repository whose base commit holds `gate_text` as `tools/ci_commit_gate.py`.
pub(super) fn fx_with_base_gate(gate_text: &str) -> Fx {
    let mut fx = Fx::new();
    fx.commit_file("tools/ci_commit_gate.py", gate_text, "base with gate");
    fx
}

pub(super) fn real_gate() -> Fx {
    fx_with_base_gate(&std::fs::read_to_string(gate()).unwrap())
}

/// A stand-in commit gate: it lists no gate path and runs `body` when it is run as a script.
pub(super) fn stub_gate(body: &str) -> String {
    stub_gate_paths("()", body)
}

pub(super) fn stub_gate_paths(paths: &str, body: &str) -> String {
    format!("import sys\nGATE_PATHS = {paths}\nif __name__ == '__main__':\n    {body}\n")
}

pub(super) fn args(list: &[&str]) -> Vec<String> {
    list.iter().map(|a| a.to_string()).collect()
}

fn check_args(fx: &Fx, head: &str, extra: &[&str]) -> Vec<String> {
    let mut a = args(&[
        "cut-check",
        "--repo",
        fx.p().to_str().unwrap(),
        "--base",
        &fx.base,
        "--head",
        head,
    ]);
    a.extend(extra.iter().map(|e| e.to_string()));
    a
}

pub(super) fn run_check(fx: &Fx, head: &str) -> Result<Vec<String>, String> {
    super::cli::run_inner(fx.p(), &check_args(fx, head, &[]), &python)
}

/// Runs `scan_commit` (the scanner the cutter will call) on `commit` in a scratch checkout of it, the gate
/// being the one of the base commit.
pub(super) fn scan(fx: &Fx, commit: &str, allow_picked: bool) -> Result<(), String> {
    let checkout = super::git::Checkout::new(fx.p(), commit).unwrap();
    let dir = tempfile::tempdir().unwrap();
    let gate = dir.path().join("gate.py");
    let text = g(
        fx.p(),
        &["show", &format!("{}:tools/ci_commit_gate.py", fx.base)],
    );
    std::fs::write(&gate, text).unwrap();
    let pinned = PinnedGate {
        gate: &gate,
        checkout: checkout.path(),
        python: &python,
    };
    scan_commit(&pinned, commit, allow_picked)
}

#[test]
fn cut_check_accepts_an_ordinary_range() {
    let fx = real_gate();
    let a = fx.hand_cut("a.txt", "");
    let b = fx.child(&a, "b.txt", "y\n", "");
    let lines = run_check(&fx, &b).unwrap();
    assert_eq!(lines, ["commit gate accepts the range"]);
    // The caller's checkout and refs are as they were.
    assert_eq!(g(fx.p(), &["branch", "--show-current"]), "main");
    assert!(g(fx.p(), &["status", "--porcelain"]).is_empty());
}

#[test]
fn cut_check_uses_the_base_gate() {
    let fx = fx_with_base_gate(&stub_gate("print('BASE-GATE', *sys.argv[1:]); sys.exit(1)"));
    let head = fx.hand_cut("a.txt", "");
    let base = fx.base.clone();
    // The working copy holds a passing gate.
    std::fs::write(
        fx.p().join("tools/ci_commit_gate.py"),
        "import sys\nGATE_PATHS = ()\nsys.exit(0)\n",
    )
    .unwrap();
    // The gate of the base commit refuses, and it is called with full commit ids.
    let a = check_args(&fx, &head[..12], &[]);
    let err = super::cli::run_inner(fx.p(), &a, &python).unwrap_err();
    assert!(
        err.contains("BASE-GATE") && err.contains(&base) && err.contains(&head),
        "{err}"
    );
    // No flag names another gate.
    let a = check_args(&fx, &head, &["--gate", "tools/ci_commit_gate.py"]);
    let err = super::cli::run_inner(fx.p(), &a, &python).unwrap_err();
    assert!(err.contains("--gate is not accepted"), "{err}");
    // A base without a gate script is refused with the reason.
    let bare = Fx::new();
    let head = bare.hand_cut("a.txt", "");
    let err = run_check(&bare, &head).unwrap_err();
    assert!(err.contains("tools/ci_commit_gate.py"), "{err}");
}

#[test]
fn cut_check_refuses_an_ungranted_gate_edit() {
    let fx = real_gate();
    let edit = fx.hand_cut("a.txt", "Gate-Edit: D-5");
    let err = run_check(&fx, &edit).unwrap_err();
    assert!(
        err.contains(&edit)
            && err.contains("Gate-Edit")
            && err.contains("grants are not accepted yet"),
        "{err}"
    );
    let flow = fx.hand_cut(".github/workflows/w.yml", "Gate-Edit: D-5");
    let err = run_check(&fx, &flow).unwrap_err();
    assert!(
        err.contains(&flow) && err.contains("grants are not accepted yet"),
        "{err}"
    );
    let bare = fx.hand_cut(".github/workflows/w.yml", "");
    let err = run_check(&fx, &bare).unwrap_err();
    assert!(
        err.contains(&bare) && err.contains("edits the gate path .github/workflows/w.yml"),
        "{err}"
    );
    // The pinned gate's wildcard: a backup copy of the gate matches `tools/ci_commit_gate.py*`.
    let bak = fx.hand_cut("tools/ci_commit_gate.py.bak", "");
    let err = run_check(&fx, &bak).unwrap_err();
    assert!(
        err.contains(&bak) && err.contains("tools/ci_commit_gate.py.bak"),
        "{err}"
    );
}

/// Python's `splitlines` breaks at U+2028, CR, form feed and NEL; Rust's `lines` does not.
#[test]
fn a_gate_edit_after_a_line_break_only_the_gate_sees_is_refused() {
    let fx = real_gate();
    for sep in ["\u{2028}", "\r", "\u{c}", "\u{85}"] {
        let head = fx.hand_cut("a.txt", &format!("Note: x{sep}Gate-Edit: D-7"));
        let err = run_check(&fx, &head).unwrap_err();
        assert!(
            err.contains(&head) && err.contains("Gate-Edit"),
            "{sep:?}: {err}"
        );
    }
}

/// The base gate omits the matrix path from `GATE_PATHS` and passes: only the GO verifier's operator class
/// knows the path, and the shared scanner refuses it in both modes with the commit and the path.
#[test]
fn cut_check_refuses_operator_class_matrix_path() {
    let fx = fx_with_base_gate(&stub_gate("sys.exit(0)"));
    let head = fx.hand_cut("deploy/ci-watch/matrix.toml", "");
    let err = run_check(&fx, &head).unwrap_err();
    assert!(
        err.contains(&head)
            && err.contains("operator-class path deploy/ci-watch/matrix.toml")
            && !err.contains("Gate-Edit line"),
        "{err}"
    );
    for allow_picked in [false, true] {
        let err = scan(&fx, &head, allow_picked).unwrap_err();
        assert!(
            err.contains(&head) && err.contains("deploy/ci-watch/matrix.toml"),
            "{err}"
        );
    }
    // A nested copy of an operator-class file name is the verifier's class too.
    let nested = fx.hand_cut("x/.gitattributes", "");
    assert!(scan(&fx, &nested, false)
        .unwrap_err()
        .contains("x/.gitattributes"));
}

/// A rename away from a protected path deletes it: renames are split into a delete and an add, so the
/// delete side is seen, for a gate path and for an operator-class path.
#[test]
fn cut_check_refuses_renaming_a_protected_path_away() {
    let mut fx = fx_with_base_gate(&stub_gate_paths("('.github/workflows/*',)", "sys.exit(0)"));
    fx.commit_file(".github/workflows/w.yml", "one\ntwo\nthree\n", "workflow");
    fx.commit_file("deploy/ci-watch/matrix.toml", "a\nb\nc\n", "matrix");
    for (from, to) in [
        (".github/workflows/w.yml", "docs/w.yml"),
        ("deploy/ci-watch/matrix.toml", "docs/matrix.toml"),
    ] {
        g(fx.p(), &["checkout", "-q", "--detach", &fx.base]);
        std::fs::create_dir_all(fx.p().join("docs")).unwrap();
        g(fx.p(), &["mv", from, to]);
        g(
            fx.p(),
            &["commit", "-q", "-m", &format!("move\n\n{TRAILERS}")],
        );
        let head = g(fx.p(), &["rev-parse", "HEAD"]);
        g(fx.p(), &["checkout", "-q", "main"]);
        let err = run_check(&fx, &head).unwrap_err();
        assert!(
            err.contains(&head) && err.contains(&format!("path {from};")),
            "{err}"
        );
    }
}

/// A grant flag changes nothing: each one is added to an otherwise valid invocation and the flag-specific
/// refusal is asserted; the file is not read.
#[test]
fn cut_check_refuses_each_grant_flag_on_a_valid_range() {
    let fx = real_gate();
    let head = fx.hand_cut("a.txt", "");
    let grants = fx.p().join(".git/grants.json");
    std::fs::write(
        &grants,
        format!(r#"[{{"d_id":"D-5","prefix":"a.txt","head":"{head}"}}]"#),
    )
    .unwrap();
    let file = grants.to_str().unwrap();
    let edits = format!("{head}=D-5");
    let run =
        |extra: &[&str]| super::cli::run_inner(fx.p(), &check_args(&fx, &head, extra), &python);
    run(&[]).unwrap();
    for (flag, value) in [("grants", file), ("gate-edits", edits.as_str())] {
        let err = run(&[&format!("--{flag}"), value]).unwrap_err();
        assert!(
            err.contains(&format!(
                "--{flag}: grants are not accepted yet: LAND-CUT-GRANT-RECORD-PRODUCER"
            )),
            "{err}"
        );
        std::fs::write(&grants, "not json").unwrap();
        let again = run(&[&format!("--{flag}"), value]).unwrap_err();
        assert_eq!(err, again);
    }
}

#[test]
fn a_hand_cherry_pick_x_branch_is_refused_and_a_source_may_carry_the_marker() {
    let fx = fx_with_base_gate(&stub_gate("sys.exit(0)"));
    let src = fx.hand_cut("a.txt", "");
    g(fx.p(), &["checkout", "-q", "--detach", &fx.base]);
    g(fx.p(), &["cherry-pick", "-x", &src]);
    let head = g(fx.p(), &["rev-parse", "HEAD"]);
    g(fx.p(), &["checkout", "-q", "main"]);
    let err = run_check(&fx, &head).unwrap_err();
    assert!(
        err.contains(&head) && err.contains("cherry picked") && err.contains("without `-x`"),
        "{err}"
    );
    // The cutter's source mode lets the marker through for the cutter to remove.
    scan(&fx, &head, true).unwrap();
    assert!(scan(&fx, &head, false)
        .unwrap_err()
        .contains("cherry picked"));
}

#[cfg(unix)]
#[test]
fn a_gate_path_that_is_not_utf8_is_refused() {
    use std::os::unix::ffi::OsStringExt;
    let fx = fx_with_base_gate(&stub_gate_paths("('.github/workflows/*',)", "sys.exit(0)"));
    let mut name = b".github/workflows/".to_vec();
    name.push(0xff);
    name.extend(b".yml");
    let name = std::ffi::OsString::from_vec(name);
    g(fx.p(), &["checkout", "-q", "--detach", &fx.base]);
    std::fs::create_dir_all(fx.p().join(".github/workflows")).unwrap();
    std::fs::write(fx.p().join(name), "x\n").unwrap();
    g(fx.p(), &["add", "-A"]);
    g(
        fx.p(),
        &["commit", "-q", "-m", &format!("edit\n\n{TRAILERS}")],
    );
    let head = g(fx.p(), &["rev-parse", "HEAD"]);
    g(fx.p(), &["checkout", "-q", "main"]);
    let err = run_check(&fx, &head).unwrap_err();
    assert!(
        err.contains(&head) && err.contains("edits the gate path"),
        "{err}"
    );
}

/// A merge of two parents: `edit` is made on top of the clean merge before it is committed.
fn merge_of(fx: &Fx, first: &str, second: &str, edit: Option<(&str, &str)>) -> String {
    g(fx.p(), &["checkout", "-q", "--detach", first]);
    g(fx.p(), &["merge", "-q", "--no-ff", "--no-commit", second]);
    if let Some((file, content)) = edit {
        let path = fx.p().join(file);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, content).unwrap();
        g(fx.p(), &["add", "-A"]);
    }
    g(
        fx.p(),
        &["commit", "-q", "-m", &format!("merge\n\n{TRAILERS}")],
    );
    let merge = g(fx.p(), &["rev-parse", "HEAD"]);
    g(fx.p(), &["checkout", "-q", "main"]);
    merge
}

/// Both parents are clean; only the merge commit's own result carries the workflow file.
#[test]
fn a_merge_commit_that_adds_a_gate_path_itself_is_refused() {
    for gate_text in [
        std::fs::read_to_string(gate()).unwrap(),
        // A gate without `_merge_edit_evidence`: the all-parent scan stands.
        stub_gate_paths("('.github/workflows/*',)", "sys.exit(0)"),
    ] {
        let fx = fx_with_base_gate(&gate_text);
        let side = fx.hand_cut("a.txt", "");
        let other = fx.hand_cut("b.txt", "");
        let merge = merge_of(&fx, &side, &other, Some((".github/workflows/w.yml", "x\n")));
        let err = run_check(&fx, &merge).unwrap_err();
        assert!(
            err.contains(&merge) && err.contains("edits the gate path .github/workflows/w.yml"),
            "{err}"
        );
    }
}

/// The base changed a workflow; a lane merges it in cleanly. The merge edits nothing itself, so only the pinned
/// gate's merge evidence lets it through, while a hand edit on top of the clean merge is refused.
#[test]
fn a_clean_merge_of_the_base_inherits_a_protected_change_without_editing_it() {
    let mut fx = real_gate();
    fx.commit_file(".github/workflows/w.yml", "one\ntwo\nthree\n", "workflow");
    let fork = fx.base.clone();
    fx.commit_file(
        ".github/workflows/w.yml",
        "one\ntwo\nthree\nfour\n",
        "workflow",
    );
    let lane = fx.child(&fork, "a.txt", "x\n", "");
    let merge = merge_of(&fx, &lane, &fx.base, None);
    run_check(&fx, &merge).unwrap();
    let edited = merge_of(
        &fx,
        &lane,
        &fx.base,
        Some((".github/workflows/w.yml", "zero\n")),
    );
    let err = run_check(&fx, &edited).unwrap_err();
    assert!(
        err.contains(&edited) && err.contains(".github/workflows/w.yml"),
        "{err}"
    );
}

/// The parents are not in the range (the base holds both), so only the merge is scanned: it resolves the
/// conflict on a workflow by keeping one parent whole, which is an edit of the workflow.
#[test]
fn cut_check_refuses_merge_conflict_kept_from_one_parent() {
    let mut fx = real_gate();
    fx.commit_file(".github/workflows/w.yml", "one\n", "workflow");
    let fork = fx.base.clone();
    let left = fx.child(&fork, ".github/workflows/w.yml", "left\n", "");
    let right = fx.child(&fork, ".github/workflows/w.yml", "right\n", "");
    // The base is a merge of both that keeps the right side; the range holds the merge that keeps the left.
    g(fx.p(), &["checkout", "-q", "--detach", &left]);
    g(
        fx.p(),
        &[
            "merge",
            "-q",
            "--no-ff",
            "-X",
            "theirs",
            "-m",
            "base merge",
            &right,
        ],
    );
    let base_merge = g(fx.p(), &["rev-parse", "HEAD"]);
    g(fx.p(), &["checkout", "-q", "--detach", &left]);
    g(
        fx.p(),
        &[
            "merge",
            "-q",
            "--no-ff",
            "-X",
            "ours",
            "-m",
            &format!("merge\n\n{TRAILERS}"),
            &right,
        ],
    );
    let head = g(fx.p(), &["rev-parse", "HEAD"]);
    g(fx.p(), &["checkout", "-q", "main"]);
    let fx = Fx {
        base: base_merge,
        ..fx
    };
    let err = run_check(&fx, &head).unwrap_err();
    assert!(
        err.contains(&head) && err.contains("edits the gate path .github/workflows/w.yml"),
        "{err}"
    );
}

/// A pinned gate that records what Git and the environment look like to it, at import and in `check`.
fn probe_gate() -> String {
    "import os, subprocess, sys\n\
     GATE_PATHS = ()\n\
     def git(*a):\n\
     \x20   return subprocess.run(['git', *a], capture_output=True, text=True).stdout.strip()\n\
     def probe(tag):\n\
     \x20   line = [tag, git('rev-parse', '--show-toplevel'), git('log', '-1', '--format=%s', os.environ['PROBE_HEAD']),\n\
     \x20       str(os.environ.get('GITHUB_EVENT_PATH')), str(os.environ.get('GIT_DIR')), git('config', 'core.attributesFile'),\n\
     \x20       str(os.environ.get('GIT_ATTR_NOSYSTEM'))]\n\
     \x20   open(os.environ['PROBE_OUT'], 'a').write(' | '.join(line) + '\\n')\n\
     probe('import')\n\
     if __name__ == '__main__':\n\
     \x20   probe('check')\n\
     \x20   sys.exit(0)\n"
        .into()
}

/// The caller's `GIT_*` variables, a replacement ref for the head and a CI event file reach neither the loader
/// nor the gate: both run in the same scratch repository and read the original objects.
#[test]
fn cut_check_ignores_git_overrides_and_replacement_refs() {
    let fx = fx_with_base_gate(&probe_gate());
    let head = fx.hand_cut("a.txt", "");
    let decoy = fx.child(&fx.base, "decoy.txt", "x\n", "");
    g(
        fx.p(),
        &["update-ref", &format!("refs/replace/{head}"), &decoy],
    );
    let out = tempfile::tempdir().unwrap();
    let probe = out.path().join("probe");
    let hostile = || {
        let mut c = python();
        c.env("PROBE_HEAD", &head)
            .env("PROBE_OUT", &probe)
            .env("GIT_DIR", fx.p().join(".git"))
            .env("GIT_WORK_TREE", fx.p())
            .env("GIT_REPLACE_REF_BASE", "refs/replace/")
            .env("GIT_OBJECT_DIRECTORY", "/nonexistent")
            .env("GITHUB_EVENT_PATH", "/nonexistent/event.json");
        c
    };
    super::cli::run_inner(fx.p(), &check_args(&fx, &head, &[]), &hostile).unwrap();
    let text = std::fs::read_to_string(&probe).unwrap();
    let rows: Vec<Vec<&str>> = text.lines().map(|l| l.split(" | ").collect()).collect();
    // The loader (import), then the gate (import and check).
    let tags: Vec<&str> = rows.iter().map(|r| r[0]).collect();
    assert_eq!(tags, ["import", "import", "check"], "{text}");
    let caller = fx.p().canonicalize().unwrap();
    for row in &rows {
        assert_eq!(row[1], rows[0][1], "{text}");
        assert_ne!(Path::new(row[1]), caller, "{text}");
        assert_eq!(row[2], "edit a.txt", "{text}");
        assert_eq!(&row[3..], ["None", "None", "/dev/null", "1"], "{text}");
    }
}

/// A module that shadows one the loader or the gate imports must not run: the scratch checkout, the gate's
/// directory and an explicit `PYTHONPATH` are all outside an isolated interpreter's path.
#[test]
fn a_checkout_local_module_cannot_shadow_the_loader() {
    let fx = fx_with_base_gate(&stub_gate("import shlex; sys.exit(0)"));
    let head = fx.hand_cut("a.txt", "");
    let shadow = tempfile::tempdir().unwrap();
    let markers = tempfile::tempdir().unwrap();
    for module in ["fnmatch", "json", "shlex"] {
        let marker = markers.path().join(module);
        std::fs::write(
            shadow.path().join(format!("{module}.py")),
            format!(
                "open({:?}, 'w').write('ran')\n",
                marker.display().to_string()
            ),
        )
        .unwrap();
    }
    let shadowed = || {
        let mut c = python();
        c.env("PYTHONPATH", shadow.path());
        c
    };
    super::cli::run_inner(fx.p(), &check_args(&fx, &head, &[]), &shadowed).unwrap();
    assert_eq!(std::fs::read_dir(markers.path()).unwrap().count(), 0);
}

/// `cargo test` runs in the crate directory: a relative repository path must still resolve in the scratch
/// checkout.
#[test]
fn a_relative_repo_path_works() {
    let fx = real_gate();
    let head = fx.hand_cut("a.txt", "");
    let cwd = std::env::current_dir().unwrap();
    let up = cwd.components().count() - 1;
    let rel = format!(
        "{}{}",
        "../".repeat(up),
        fx.p().strip_prefix("/").unwrap().display()
    );
    assert!(!rel.starts_with('/') && Path::new(&rel).join(".git").exists());
    let a = args(&[
        "cut-check",
        "--repo",
        &rel,
        "--base",
        &fx.base,
        "--head",
        &head,
    ]);
    super::cli::run_inner(fx.p(), &a, &python).unwrap();
}
