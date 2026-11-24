use super::build_tests::g;
use super::cut_grant_tests::{args, fx_with_base_gate, hand_cut_with, run_check, write_grants};
use super::cut_tests::{gate, python, TRAILERS};

#[test]
fn a_whitespace_only_difference_from_the_source_is_not_its_replay() {
    let fx = fx_with_base_gate(&std::fs::read_to_string(gate()).unwrap());
    let real = fx.source(".github/workflows/w.yml", TRAILERS);
    let grants = write_grants(&fx, "g.json", &[("D-5", ".github/workflows/", &real)]);
    // The source writes "x\n"; a stable patch id ignores the added trailing space, the replay does not.
    let head = hand_cut_with(
        &fx,
        ".github/workflows/w.yml",
        "x \n",
        "Gate-Edit: D-5",
        &real,
    );
    let err = run_check(&fx, &head, &grants).unwrap_err();
    assert!(err.contains("not the exact replay of source"), "{err}");
    g(fx.p(), &["branch", "-q", "-D", "hand"]);
    // The exact replay passes.
    let head = hand_cut_with(
        &fx,
        ".github/workflows/w.yml",
        "x\n",
        "Gate-Edit: D-5",
        &real,
    );
    run_check(&fx, &head, &grants).unwrap();
}

#[test]
fn every_gate_edit_d_id_is_checked_against_the_changed_paths() {
    let fx = fx_with_base_gate(&std::fs::read_to_string(gate()).unwrap());
    let real = fx.source(".github/workflows/w.yml", TRAILERS);
    let head = hand_cut_with(
        &fx,
        ".github/workflows/w.yml",
        "x\n",
        "Gate-Edit: D-5\nGate-Edit: D-6",
        &real,
    );
    // D-6 has a row for the source, but only for an unrelated path.
    let grants = write_grants(
        &fx,
        "g.json",
        &[
            ("D-5", ".github/workflows/", &real),
            ("D-6", "docs/", &real),
        ],
    );
    let err = run_check(&fx, &head, &grants).unwrap_err();
    assert!(
        err.contains("D-6") && err.contains("no grant covers"),
        "{err}"
    );
}

#[cfg(unix)]
#[test]
fn a_path_that_is_not_utf8_never_matches_a_prefix_through_lossy_decoding() {
    use std::os::unix::ffi::OsStringExt;
    let fx = fx_with_base_gate(&std::fs::read_to_string(gate()).unwrap());
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
    // The prefix is the lossy rendering of that path; the raw bytes differ.
    let grants = write_grants(
        &fx,
        "g.json",
        &[("D-5", ".github/workflows/\u{fffd}.yml", &src)],
    );
    let a = args(&[
        "cut",
        "--repo",
        fx.p().to_str().unwrap(),
        "--base",
        &fx.base,
        "--branch",
        "slice/a",
        "--commits",
        &src,
        "--gate-edits",
        &format!("{src}=D-5"),
        "--grants",
        grants.to_str().unwrap(),
    ]);
    let err = super::cli::run_inner(fx.p(), &a, &python).unwrap_err();
    assert!(err.contains("no grant covers"), "{err}");
    assert!(g(fx.p(), &["branch", "--list", "slice/a"]).is_empty());
}

#[test]
fn a_gate_edit_after_a_line_break_only_the_gate_sees_is_still_checked() {
    let fx = fx_with_base_gate(&std::fs::read_to_string(gate()).unwrap());
    let real = fx.source("a.txt", TRAILERS);
    let grants = write_grants(&fx, "g.json", &[("D-5", ".github/workflows/", &real)]);
    // Python's splitlines breaks at U+2028, CR and form feed; Rust's lines() does not.
    for sep in ["\u{2028}", "\r", "\u{c}", "\u{85}"] {
        let note = format!("Note: x{sep}Gate-Edit: D-7");
        let head = hand_cut_with(&fx, "a.txt", "x\n", &note, &real);
        let err = run_check(&fx, &head, &grants).unwrap_err();
        assert!(err.contains("D-7"), "{sep:?}: {err}");
        g(fx.p(), &["branch", "-q", "-D", "hand"]);
    }
}

#[test]
fn py_lines_breaks_where_python_splitlines_does() {
    use super::cut_grants::py_lines;
    assert_eq!(
        py_lines("a\r\nb\rc\u{2028}d\u{c}e\n"),
        ["a", "b", "c", "d", "e"]
    );
    assert_eq!(py_lines("a\n\nb"), ["a", "", "b"]);
    assert!(py_lines("").is_empty());
}

/// Repository-local merge settings (a driver chosen in `info/attributes`) must not change the replay.
#[test]
fn a_repository_merge_driver_cannot_make_an_unrelated_commit_a_replay() {
    let fx = fx_with_base_gate(&std::fs::read_to_string(gate()).unwrap());
    let commit_file = |content: &str, msg: &str| {
        std::fs::write(fx.p().join("a.txt"), content).unwrap();
        g(fx.p(), &["add", "-A"]);
        g(fx.p(), &["commit", "-q", "-m", msg]);
        g(fx.p(), &["rev-parse", "HEAD"])
    };
    let ten = |first: &str, last: &str| {
        let mut lines: Vec<String> = (2..10).map(|n| n.to_string()).collect();
        lines.insert(0, first.into());
        lines.push(last.into());
        lines.join("\n") + "\n"
    };
    let p0 = commit_file(&ten("1", "10"), "p0");
    let source = commit_file(&ten("one", "10"), &format!("source\n\n{TRAILERS}"));
    g(fx.p(), &["checkout", "-q", "--detach", &p0]);
    let parent = commit_file(&ten("1", "ten"), "parent");
    // An empty commit on `parent` that claims to be the replay of `source`.
    let msg = format!("claim\n\n{TRAILERS}\nGate-Edit: D-5\nPicked-From: {source}");
    g(fx.p(), &["commit", "-q", "--allow-empty", "-m", &msg]);
    let claim = g(fx.p(), &["rev-parse", "HEAD"]);
    g(fx.p(), &["checkout", "-q", "main"]);
    // The poisoned repository: a driver that keeps "ours" for every content merge.
    g(fx.p(), &["config", "merge.keep.driver", "true"]);
    std::fs::write(fx.p().join(".git/info/attributes"), "* merge=keep\n").unwrap();
    let grants = write_grants(&fx, "g.json", &[("D-5", "a.txt", &source)]);
    let a = args(&[
        "cut-check",
        "--repo",
        fx.p().to_str().unwrap(),
        "--base",
        &parent,
        "--head",
        &claim,
        "--grants",
        grants.to_str().unwrap(),
    ]);
    let err = super::cli::run_inner(fx.p(), &a, &python).unwrap_err();
    assert!(err.contains("not the exact replay of source"), "{err}");
}

/// `path` as seen from the test's working directory, written relative with the fewest `..` (so it does not
/// resolve from anywhere else, such as a scratch directory).
fn relative(path: &std::path::Path) -> String {
    let cwd = std::env::current_dir().unwrap();
    let (a, b): (Vec<_>, Vec<_>) = (cwd.components().collect(), path.components().collect());
    let common = a.iter().zip(&b).take_while(|(x, y)| x == y).count();
    let mut parts: Vec<String> = vec!["..".into(); a.len() - common];
    parts.extend(
        b[common..]
            .iter()
            .map(|c| c.as_os_str().to_string_lossy().into_owned()),
    );
    parts.join("/")
}

#[test]
fn a_relative_repo_path_works_for_cut_and_for_a_granted_cut_check() {
    let fx = fx_with_base_gate(&std::fs::read_to_string(gate()).unwrap());
    let rel = relative(fx.p());
    assert!(!rel.starts_with('/'));
    // cut-check with a grant reaches the replay clone.
    let real = fx.source(".github/workflows/w.yml", TRAILERS);
    let head = hand_cut_with(
        &fx,
        ".github/workflows/w.yml",
        "x\n",
        "Gate-Edit: D-5",
        &real,
    );
    let grants = write_grants(&fx, "g.json", &[("D-5", ".github/workflows/", &real)]);
    let a = args(&[
        "cut-check",
        "--repo",
        &rel,
        "--base",
        &fx.base,
        "--head",
        &head,
        "--grants",
        grants.to_str().unwrap(),
    ]);
    super::cli::run_inner(fx.p(), &a, &python).unwrap();
    // cut with a granted gate edit.
    let a = args(&[
        "cut",
        "--repo",
        &rel,
        "--base",
        &fx.base,
        "--branch",
        "slice/rel",
        "--commits",
        &real,
        "--gate-edits",
        &format!("{real}=D-5"),
        "--grants",
        grants.to_str().unwrap(),
    ]);
    super::cli::run_inner(fx.p(), &a, &python).unwrap();
    assert!(!g(fx.p(), &["rev-parse", "slice/rel"]).is_empty());
}

#[test]
fn a_cut_leaves_fetch_head_and_foreign_hold_refs_alone() {
    let fx = fx_with_base_gate(&std::fs::read_to_string(gate()).unwrap());
    let src = fx.source("a.txt", TRAILERS);
    let fetch_head = fx.p().join(".git/FETCH_HEAD");
    std::fs::write(&fetch_head, "marker\n").unwrap();
    // A hold ref of another run that happens to carry this process id's name must survive a failed cut.
    let foreign = format!("refs/plangraph/cut-hold/{}", std::process::id());
    g(fx.p(), &["update-ref", &foreign, &fx.base]);
    let flow = fx.source(".github/workflows/w.yml", TRAILERS);
    let a = args(&[
        "cut",
        "--repo",
        fx.p().to_str().unwrap(),
        "--base",
        &fx.base,
        "--branch",
        "slice/bad",
        "--commits",
        &flow,
    ]);
    super::cli::run_inner(fx.p(), &a, &python).unwrap_err();
    assert_eq!(g(fx.p(), &["rev-parse", &foreign]), fx.base);
    // A good cut adopts the head without touching FETCH_HEAD and leaves no hold ref of its own.
    let a = args(&[
        "cut",
        "--repo",
        fx.p().to_str().unwrap(),
        "--base",
        &fx.base,
        "--branch",
        "slice/ok",
        "--commits",
        &src,
    ]);
    super::cli::run_inner(fx.p(), &a, &python).unwrap();
    assert_eq!(std::fs::read_to_string(&fetch_head).unwrap(), "marker\n");
    let holds = g(
        fx.p(),
        &[
            "for-each-ref",
            "--format=%(refname)",
            "refs/plangraph/cut-hold",
        ],
    );
    assert_eq!(holds, foreign, "{holds}");
}
