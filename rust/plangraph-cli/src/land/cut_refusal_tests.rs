//! Cut and cut-check accept no grant: a gate path or a `Gate-Edit` line is refused before any pick.
use super::build_tests::g;
use super::cut_gate::py_lines;
use super::cut_tests::{gate, python, Fx, TRAILERS};

/// A repository whose base commit holds `gate_text` as `tools/ci_commit_gate.py`.
pub(super) fn fx_with_base_gate(gate_text: &str) -> Fx {
    let fx = Fx::new();
    std::fs::create_dir_all(fx.p().join("tools")).unwrap();
    std::fs::write(fx.p().join("tools/ci_commit_gate.py"), gate_text).unwrap();
    g(fx.p(), &["add", "-A"]);
    g(fx.p(), &["commit", "-q", "-m", "base with gate"]);
    let base = g(fx.p(), &["rev-parse", "HEAD"]);
    Fx { base, ..fx }
}

fn real_gate() -> Fx {
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

/// A hand-cut branch `hand` of one commit that changes `file` to `content` and carries `extra` in its message.
pub(super) fn hand_cut(fx: &Fx, file: &str, content: &str, extra: &str) -> String {
    g(fx.p(), &["checkout", "-q", "-b", "hand", &fx.base]);
    let path = fx.p().join(file);
    std::fs::create_dir_all(path.parent().unwrap()).unwrap();
    std::fs::write(path, content).unwrap();
    g(fx.p(), &["add", "-A"]);
    g(
        fx.p(),
        &[
            "commit",
            "-q",
            "-m",
            &format!("edit\n\n{TRAILERS}\n{extra}"),
        ],
    );
    let head = g(fx.p(), &["rev-parse", "HEAD"]);
    g(fx.p(), &["checkout", "-q", "main"]);
    head
}

pub(super) fn run_check(fx: &Fx, head: &str) -> Result<Vec<String>, String> {
    let a = args(&[
        "cut-check",
        "--repo",
        fx.p().to_str().unwrap(),
        "--base",
        &fx.base,
        "--head",
        head,
    ]);
    super::cli::run_inner(fx.p(), &a, &python)
}

pub(super) fn run_cut(
    fx: &Fx,
    branch: &str,
    commit: &str,
    extra: &[&str],
) -> Result<Vec<String>, String> {
    let mut a = args(&[
        "cut",
        "--repo",
        fx.p().to_str().unwrap(),
        "--base",
        &fx.base,
        "--branch",
        branch,
        "--commits",
        commit,
    ]);
    a.extend(extra.iter().map(|e| e.to_string()));
    super::cli::run_inner(fx.p(), &a, &python)
}

#[test]
fn cut_uses_the_base_gate() {
    let fx = fx_with_base_gate(&stub_gate("print('BASE-GATE', *sys.argv[1:]); sys.exit(1)"));
    let src = fx.source("a.txt", TRAILERS);
    let base = fx.base.clone();
    // The working copy holds a passing gate.
    std::fs::write(
        fx.p().join("tools/ci_commit_gate.py"),
        "import sys\nsys.exit(0)\n",
    )
    .unwrap();
    // The gate of the base commit refuses, and it is called with full commit ids.
    let err = run_cut(&fx, "slice/a", &src[..12], &[]).unwrap_err();
    assert!(err.contains("BASE-GATE") && err.contains(&base), "{err}");
    assert!(g(fx.p(), &["branch", "--list", "slice/a"]).is_empty());
    // No flag names another gate.
    let err = run_cut(&fx, "slice/a", &src, &["--gate", "tools/ci_commit_gate.py"]).unwrap_err();
    assert!(err.contains("--gate is not accepted"), "{err}");
    // An abbreviated input is recorded as the full id.
    let ok = fx_with_base_gate(&stub_gate("sys.exit(0)"));
    let src = ok.source("a.txt", TRAILERS);
    run_cut(&ok, "slice/a", &src[..12], &[]).unwrap();
    assert!(ok
        .message("slice/a")
        .contains(&format!("Picked-From: {src}")));
}

#[test]
fn a_visible_gate_edit_in_a_source_is_refused_before_any_pick() {
    let fx = Fx::new();
    let src = fx.source("a.txt", &format!("{TRAILERS}\nGate-Edit: D-77"));
    let err = fx.cut(&[src]).unwrap_err();
    assert!(
        err.contains("Gate-Edit") && err.contains("grants are not accepted yet"),
        "{err}"
    );
    assert!(g(fx.p(), &["branch", "--list", "slice/a"]).is_empty());
    assert_eq!(fx.worktrees(), 1);
}

#[test]
fn a_gate_edit_git_does_not_parse_in_the_source_is_refused_too() {
    let fx = Fx::new();
    // The `(cherry picked ...)` paragraph is the last one, so git finds no Gate-Edit trailer in the source;
    // cutting would drop it and the paragraph before becomes the trailer block the commit gate reads.
    let src = fx.source(
        "a.txt",
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
    let err = fx.cut(std::slice::from_ref(&src)).unwrap_err();
    assert!(err.contains(&src) && err.contains("Gate-Edit"), "{err}");
    assert!(g(fx.p(), &["branch", "--list", "slice/a"]).is_empty());
}

#[test]
fn cut_check_refuses_a_gate_edit_line_and_a_gate_path() {
    let fx = real_gate();
    let edit = hand_cut(&fx, "a.txt", "x\n", "Gate-Edit: D-5");
    assert!(run_check(&fx, &edit)
        .unwrap_err()
        .contains("grants are not accepted yet"));
    g(fx.p(), &["branch", "-q", "-D", "hand"]);
    let flow = hand_cut(&fx, ".github/workflows/w.yml", "x\n", "Gate-Edit: D-5");
    assert!(run_check(&fx, &flow)
        .unwrap_err()
        .contains("grants are not accepted yet"));
    g(fx.p(), &["branch", "-q", "-D", "hand"]);
    let bare = hand_cut(&fx, ".github/workflows/w.yml", "x\n", "");
    let err = run_check(&fx, &bare).unwrap_err();
    assert!(
        err.contains(&bare) && err.contains("edits the gate path"),
        "{err}"
    );
    g(fx.p(), &["branch", "-q", "-D", "hand"]);
    // A plain commit still passes.
    let plain = hand_cut(&fx, "a.txt", "x\n", "");
    run_check(&fx, &plain).unwrap();
}

#[test]
fn a_gate_edit_after_a_line_break_only_the_gate_sees_is_refused() {
    let fx = real_gate();
    // Python's splitlines breaks at U+2028, CR, form feed and NEL; Rust's lines() does not.
    for sep in ["\u{2028}", "\r", "\u{c}", "\u{85}"] {
        let head = hand_cut(&fx, "a.txt", "x\n", &format!("Note: x{sep}Gate-Edit: D-7"));
        let err = run_check(&fx, &head).unwrap_err();
        assert!(err.contains("Gate-Edit"), "{sep:?}: {err}");
        g(fx.p(), &["branch", "-q", "-D", "hand"]);
    }
}

#[test]
fn py_lines_breaks_where_python_splitlines_does() {
    assert_eq!(
        py_lines("a\r\nb\rc\u{2028}d\u{c}e\n"),
        ["a", "b", "c", "d", "e"]
    );
    assert_eq!(py_lines("a\n\nb"), ["a", "", "b"]);
    assert!(py_lines("").is_empty());
}

#[cfg(unix)]
#[test]
fn a_gate_path_that_is_not_utf8_is_refused() {
    use std::os::unix::ffi::OsStringExt;
    let fx = Fx::new();
    g(fx.p(), &["checkout", "-q", "--detach", &fx.base]);
    let mut name = b".github/workflows/".to_vec();
    name.push(0xff);
    name.extend(b".yml");
    std::fs::create_dir_all(fx.p().join(".github/workflows")).unwrap();
    std::fs::write(fx.p().join(std::ffi::OsString::from_vec(name)), "x\n").unwrap();
    g(fx.p(), &["add", "-A"]);
    g(
        fx.p(),
        &["commit", "-q", "-m", &format!("edit\n\n{TRAILERS}")],
    );
    let src = g(fx.p(), &["rev-parse", "HEAD"]);
    g(fx.p(), &["checkout", "-q", "main"]);
    let err = fx.cut(std::slice::from_ref(&src)).unwrap_err();
    assert!(
        err.contains(&src) && err.contains("edits the gate path"),
        "{err}"
    );
}

#[test]
fn a_merge_commit_that_adds_a_gate_path_itself_is_refused_by_the_check() {
    let fx = real_gate();
    // Both parents are clean; only the merge commit's own result carries the workflow file.
    let side = fx.source("a.txt", TRAILERS);
    g(fx.p(), &["checkout", "-q", "-b", "hand", &fx.base]);
    std::fs::write(fx.p().join("b.txt"), "y\n").unwrap();
    g(fx.p(), &["add", "-A"]);
    g(fx.p(), &["commit", "-q", "-m", &format!("b\n\n{TRAILERS}")]);
    g(fx.p(), &["merge", "-q", "--no-ff", "--no-commit", &side]);
    std::fs::create_dir_all(fx.p().join(".github/workflows")).unwrap();
    std::fs::write(fx.p().join(".github/workflows/w.yml"), "x\n").unwrap();
    g(fx.p(), &["add", "-A"]);
    g(
        fx.p(),
        &["commit", "-q", "-m", &format!("merge\n\n{TRAILERS}")],
    );
    let merge = g(fx.p(), &["rev-parse", "HEAD"]);
    g(fx.p(), &["checkout", "-q", "main"]);
    let err = run_check(&fx, &merge).unwrap_err();
    assert!(
        err.contains(&merge) && err.contains("edits the gate path"),
        "{err}"
    );
}

#[test]
fn a_path_the_pinned_gate_matches_by_wildcard_is_refused_before_any_pick() {
    let fx = Fx::new();
    // GATE_PATHS has 'tools/ci_commit_gate.py*': a backup copy matches the gate but not the router's list.
    let src = fx.source("tools/ci_commit_gate.py.bak", TRAILERS);
    let err = fx.cut(std::slice::from_ref(&src)).unwrap_err();
    // The diagnostic names the source commit, so the refusal came before the pick (a picked commit has
    // another id and the final check would name that one).
    assert!(
        err.contains(&src) && err.contains("tools/ci_commit_gate.py.bak"),
        "{err}"
    );
    assert!(err.contains("grants are not accepted yet"), "{err}");
    assert!(g(fx.p(), &["branch", "--list", "slice/a"]).is_empty());
    assert_eq!(fx.worktrees(), 1);
}

/// A well-formed grant file for the very commits, however it is passed, enables nothing: each forbidden flag
/// is added to an otherwise valid invocation and the flag-specific refusal is asserted.
#[test]
fn a_well_formed_grant_file_changes_nothing() {
    let fx = real_gate();
    let plain = fx.source("a.txt", TRAILERS);
    let grants = fx.p().join(".git/grants.json");
    std::fs::write(
        &grants,
        format!(r#"[{{"d_id":"D-5","prefix":"a.txt","head":"{plain}"}}]"#),
    )
    .unwrap();
    let file = grants.to_str().unwrap();
    let edits = format!("{plain}=D-5");
    for (flag, value) in [("grants", file), ("gate-edits", edits.as_str())] {
        let err = run_cut(&fx, "slice/a", &plain, &[&format!("--{flag}"), value]).unwrap_err();
        assert!(
            err.contains(&format!(
                "--{flag}: grants are not accepted yet: LAND-CUT-GRANT-RECORD-PRODUCER"
            )),
            "{err}"
        );
        assert!(g(fx.p(), &["branch", "--list", "slice/a"]).is_empty());
    }
    // The same invocation without the flag cuts.
    run_cut(&fx, "slice/a", &plain, &[]).unwrap();
    let hand = hand_cut(&fx, "b.txt", "x\n", "");
    let args_for = |extra: &[&str]| {
        let mut a = args(&[
            "cut-check",
            "--repo",
            fx.p().to_str().unwrap(),
            "--base",
            &fx.base,
            "--head",
            &hand,
        ]);
        a.extend(extra.iter().map(|e| e.to_string()));
        super::cli::run_inner(fx.p(), &a, &python)
    };
    args_for(&[]).unwrap();
    let err = args_for(&["--grants", file]).unwrap_err();
    assert!(
        err.contains("--grants: grants are not accepted yet"),
        "{err}"
    );
    // The file is not read: a malformed one gives the same refusal.
    std::fs::write(&grants, "not json").unwrap();
    let err = args_for(&["--grants", file]).unwrap_err();
    assert!(
        err.contains("--grants: grants are not accepted yet"),
        "{err}"
    );
}
