//! What a cut does to the caller's repository state: relative paths, FETCH_HEAD and refs.
use super::build_tests::g;
use super::cut::{cut, Cut};
use super::cut_refusal_tests::{args, fx_with_base_gate};
use super::cut_tests::{gate, python, Fx, TRAILERS};

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

fn real_gate() -> Fx {
    fx_with_base_gate(&std::fs::read_to_string(gate()).unwrap())
}

#[test]
fn a_relative_repo_path_works_for_cut_and_for_cut_check() {
    let fx = real_gate();
    let rel = relative(fx.p());
    assert!(!rel.starts_with('/'));
    let src = fx.source("a.txt", TRAILERS);
    let a = args(&[
        "cut",
        "--repo",
        &rel,
        "--base",
        &fx.base,
        "--branch",
        "slice/rel",
        "--commits",
        &src,
    ]);
    super::cli::run_inner(fx.p(), &a, &python).unwrap();
    let head = g(fx.p(), &["rev-parse", "slice/rel"]);
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

#[test]
fn a_cut_leaves_fetch_head_alone_and_makes_no_ref_but_its_branch() {
    let fx = real_gate();
    let src = fx.source("a.txt", TRAILERS);
    let fetch_head = fx.p().join(".git/FETCH_HEAD");
    std::fs::write(&fetch_head, "marker\n").unwrap();
    // Refs another run holds under the names an earlier version used must survive.
    let pid = std::process::id();
    let foreign = [
        format!("refs/plangraph/cut-hold/{pid}"),
        format!("refs/plangraph/cut-hold/{pid}-{}", "1".repeat(40)),
    ];
    for r in &foreign {
        g(fx.p(), &["update-ref", r, &fx.base]);
    }
    fx.cut_to("slice/ok", &[src], &gate(), &python).unwrap();
    assert_eq!(std::fs::read_to_string(&fetch_head).unwrap(), "marker\n");
    for r in &foreign {
        assert_eq!(g(fx.p(), &["rev-parse", r]), fx.base);
    }
    let refs = g(
        fx.p(),
        &[
            "for-each-ref",
            "--format=%(refname)",
            "refs/plangraph",
            "refs/heads/slice",
        ],
    );
    let mut want: Vec<String> = foreign.to_vec();
    want.push("refs/heads/slice/ok".into());
    want.sort();
    assert_eq!(
        refs.lines().map(str::to_owned).collect::<Vec<_>>(),
        want,
        "{refs}"
    );
}

/// The exact name an earlier version gave its hold ref (process id and the picked head the gate is given) is
/// seeded by a gate stub that sees that head, before the cut can adopt it; the ref must survive the cut.
#[test]
fn a_ref_at_the_name_an_old_hold_ref_would_take_survives_the_cut() {
    let fx = real_gate();
    let src = fx.source("a.txt", TRAILERS);
    let stub = fx.p().join(".git/seed_gate.py");
    std::fs::write(
        &stub,
        "import os, subprocess, sys\n\
         GATE_PATHS = ()\n\
         if __name__ == '__main__':\n\
         \x20   head = sys.argv[sys.argv.index('--head') + 1]\n\
         \x20   ref = 'refs/plangraph/cut-hold/%d-%s' % (os.getppid(), head)\n\
         \x20   subprocess.run(['git', '-C', os.environ['SEED_REPO'], 'update-ref', ref, os.environ['SEED_BASE']], check=True)\n",
    )
    .unwrap();
    let python_seeding = || {
        let mut c = python();
        c.env("SEED_REPO", fx.p()).env("SEED_BASE", &fx.base);
        c
    };
    cut(
        &Cut {
            repo: fx.p(),
            base: &fx.base,
            branch: "slice/a",
            commits: &[src],
            gate: &stub,
        },
        &python_seeding,
    )
    .unwrap();
    let head = g(fx.p(), &["rev-parse", "slice/a"]);
    let seeded = format!("refs/plangraph/cut-hold/{}-{head}", std::process::id());
    assert_eq!(g(fx.p(), &["rev-parse", &seeded]), fx.base);
}
