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
