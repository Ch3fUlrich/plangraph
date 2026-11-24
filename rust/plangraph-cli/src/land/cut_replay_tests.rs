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
