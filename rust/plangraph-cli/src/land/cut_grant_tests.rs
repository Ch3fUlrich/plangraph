use super::build_tests::g;
use super::cut::*;
use super::cut_tests::{gate, python, Fx, TRAILERS};
use super::go_verify::Grant;
use std::collections::BTreeMap;

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

pub(super) fn args(list: &[&str]) -> Vec<String> {
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
        protected_write(&path, &grants);
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

/// The record as the router writes it: mode 0600.
fn protected_write(path: &std::path::Path, grants: &[Grant]) {
    std::fs::write(path, serde_json::to_vec(grants).unwrap()).unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600)).unwrap();
    }
}

pub(super) fn write_grants(fx: &Fx, name: &str, rows: &[(&str, &str, &str)]) -> std::path::PathBuf {
    // Inside .git: not part of any commit, and gone with the fixture.
    let path = fx.p().join(".git").join(name);
    let grants: Vec<Grant> = rows
        .iter()
        .map(|(d, prefix, head)| Grant {
            d_id: d.to_string(),
            prefix: prefix.to_string(),
            head: head.to_string(),
        })
        .collect();
    protected_write(&path, &grants);
    path
}

/// A hand-cut branch `hand` of one commit that changes `file` to `content`, carries `gate_edits` (Gate-Edit
/// lines) and `Picked-From: claimed`.
pub(super) fn hand_cut_with(
    fx: &Fx,
    file: &str,
    content: &str,
    gate_edits: &str,
    claimed: &str,
) -> String {
    g(fx.p(), &["checkout", "-q", "-b", "hand", &fx.base]);
    let path = fx.p().join(file);
    std::fs::create_dir_all(path.parent().unwrap()).unwrap();
    std::fs::write(path, content).unwrap();
    g(fx.p(), &["add", "-A"]);
    let msg = format!("edit\n\n{TRAILERS}\n{gate_edits}\nPicked-From: {claimed}");
    g(fx.p(), &["commit", "-q", "-m", &msg]);
    let head = g(fx.p(), &["rev-parse", "HEAD"]);
    g(fx.p(), &["checkout", "-q", "main"]);
    head
}

fn hand_cut(fx: &Fx, file: &str, claimed: &str) -> String {
    hand_cut_with(fx, file, "other\n", "Gate-Edit: D-5", claimed)
}

pub(super) fn run_check(
    fx: &Fx,
    head: &str,
    grants: &std::path::Path,
) -> Result<Vec<String>, String> {
    let a = args(&[
        "cut-check",
        "--repo",
        fx.p().to_str().unwrap(),
        "--base",
        &fx.base,
        "--head",
        head,
        "--grants",
        grants.to_str().unwrap(),
    ]);
    super::cli::run_inner(fx.p(), &a, &python)
}

#[test]
fn a_picked_from_that_names_another_edit_does_not_borrow_its_grant() {
    let fx = fx_with_base_gate(&std::fs::read_to_string(gate()).unwrap());
    let real = fx.source(".github/workflows/w.yml", TRAILERS);
    // Same path, different content: the grant is for `real`, the hand commit claims to be it.
    let head = hand_cut(&fx, ".github/workflows/w.yml", &real);
    let grants = write_grants(&fx, "g.json", &[("D-5", ".github/workflows/", &real)]);
    let err = run_check(&fx, &head, &grants).unwrap_err();
    assert!(err.contains("not the exact replay of source"), "{err}");
}

#[test]
fn a_picked_from_that_is_not_in_the_repository_is_refused() {
    let fx = fx_with_base_gate(&std::fs::read_to_string(gate()).unwrap());
    let ghost = "1".repeat(40);
    let head = hand_cut(&fx, ".github/workflows/w.yml", &ghost);
    let grants = write_grants(&fx, "g.json", &[("D-5", ".github/workflows/", &ghost)]);
    let err = run_check(&fx, &head, &grants).unwrap_err();
    assert!(err.contains("is not in this repository"), "{err}");
}

#[test]
fn every_changed_path_needs_a_prefix_including_a_quoted_one() {
    let fx = fx_with_base_gate(&std::fs::read_to_string(gate()).unwrap());
    let src = fx.source(
        ".github/workflows/\u{fc}.yml",
        &format!("{TRAILERS}\nGate-Edit: D-5"),
    );
    let flow_only = write_grants(&fx, "a.json", &[("D-5", "docs/", &src)]);
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
        "--grants",
        flow_only.to_str().unwrap(),
    ]);
    let err = super::cli::run_inner(fx.p(), &a, &python).unwrap_err();
    assert!(err.contains("no grant covers"), "{err}");
    assert!(g(fx.p(), &["branch", "--list", "slice/a"]).is_empty());
    let both = write_grants(&fx, "b.json", &[("D-5", ".github/workflows/", &src)]);
    let mut a = a;
    *a.last_mut().unwrap() = both.to_str().unwrap().to_owned();
    super::cli::run_inner(fx.p(), &a, &python).unwrap();
}

#[test]
fn a_merge_commit_carrying_a_gate_edit_is_refused_by_the_check() {
    let fx = fx_with_base_gate(&std::fs::read_to_string(gate()).unwrap());
    let a = fx.source("a.txt", TRAILERS);
    g(fx.p(), &["checkout", "-q", "--detach", &fx.base]);
    g(
        fx.p(),
        &[
            "merge",
            "-q",
            "--no-ff",
            "-m",
            &format!("merge\n\n{TRAILERS}\nGate-Edit: D-5\nPicked-From: {a}"),
            &a,
        ],
    );
    let merge = g(fx.p(), &["rev-parse", "HEAD"]);
    g(fx.p(), &["checkout", "-q", "main"]);
    let grants = write_grants(&fx, "g.json", &[("D-5", "a.txt", &a)]);
    let err = run_check(&fx, &merge, &grants).unwrap_err();
    assert!(err.contains("not a single-parent commit"), "{err}");
}

#[cfg(unix)]
#[test]
fn a_grant_record_anyone_can_write_or_that_is_a_link_is_refused() {
    use std::os::unix::fs::PermissionsExt;
    let fx = fx_with_base_gate(&std::fs::read_to_string(gate()).unwrap());
    let src = fx.source("a.txt", TRAILERS);
    let head = fx.source("b.txt", TRAILERS);
    let rows = write_grants(&fx, "g.json", &[("D-1", "a.txt", &src)]);
    std::fs::set_permissions(&rows, std::fs::Permissions::from_mode(0o664)).unwrap();
    let err = run_check(&fx, &head, &rows).unwrap_err();
    assert!(err.contains("writable by group or others"), "{err}");
    std::fs::set_permissions(&rows, std::fs::Permissions::from_mode(0o600)).unwrap();
    let link = fx.p().join("link.json");
    std::os::unix::fs::symlink(&rows, &link).unwrap();
    let err = run_check(&fx, &head, &link).unwrap_err();
    assert!(err.contains("not a regular file"), "{err}");
}
