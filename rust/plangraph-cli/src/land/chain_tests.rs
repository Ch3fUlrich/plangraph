//! The push gate's chain check with the new main merged into an open train (parallel trains).
use super::build::{build, publish, verify_manifest_with};
use super::build_tests::{g, part, Fx};
use super::chain::check_push;
use super::train::Manifest;
use std::path::Path;

/// A two-part train on the base, with `origin/main` still at the base.
fn open_train(fx: &Fx) -> (Manifest, Vec<super::train::PartIn>, std::path::PathBuf) {
    let a = fx.commit(&fx.base(), "a.txt", "a\n");
    let b = fx.commit(&fx.base(), "b.txt", "b\n");
    let parts = vec![part(1, &a, None), part(2, &b, None)];
    let m = build(&fx.input(parts.clone(), "", None))
        .unwrap()
        .manifest
        .unwrap();
    publish(fx.path(), "land/build/b1", &m).unwrap();
    g(
        fx.path(),
        &["update-ref", "refs/remotes/origin/main", &fx.base()],
    );
    let heads = fx.path().join(".git").join("heads.txt");
    std::fs::write(&heads, format!("{}\n{}\n", a, b)).unwrap();
    (m, parts, heads)
}

/// Another train landed: main moves by one landing merge (first parent the old main).
fn land_other(fx: &Fx) -> String {
    let side = fx.commit(&fx.base(), "landed.txt", "landed\n");
    g(fx.path(), &["checkout", "-q", "--detach", &fx.base()]);
    g(
        fx.path(),
        &["merge", "--no-ff", "-q", "-m", "Landing train 1", &side],
    );
    let landed = g(fx.path(), &["rev-parse", "HEAD"]);
    g(fx.path(), &["checkout", "-q", "main"]);
    g(
        fx.path(),
        &["update-ref", "refs/remotes/origin/main", &landed],
    );
    landed
}

fn remerge(repo: &Path, tip: &str, main: &str) -> String {
    g(repo, &["checkout", "-q", "--detach", tip]);
    g(
        repo,
        &[
            "merge",
            "--no-ff",
            "-q",
            "-m",
            "Merge main into train",
            main,
        ],
    );
    let head = g(repo, &["rev-parse", "HEAD"]);
    g(repo, &["checkout", "-q", "main"]);
    head
}

#[test]
fn an_open_train_that_merged_the_new_main_passes_the_push_chain_check() {
    let fx = Fx::new();
    let (m, _, heads) = open_train(&fx);
    check_push(fx.path(), &m.train_sha, &heads).unwrap();
    let landed = land_other(&fx);
    // Main moved but the train did not: unchanged behaviour.
    check_push(fx.path(), &m.train_sha, &heads).unwrap();
    let tip = remerge(fx.path(), &m.train_sha, &landed);
    check_push(fx.path(), &tip, &heads).unwrap();
}

#[test]
fn a_second_parent_that_is_not_new_first_parent_history_of_main_is_refused() {
    let fx = Fx::new();
    let (m, _, heads) = open_train(&fx);
    let landed = land_other(&fx);
    // The side commit a landing merge brought in is landed, but it is not main's own line.
    let side = g(fx.path(), &["rev-parse", &format!("{landed}^2")]);
    let tip = remerge(fx.path(), &m.train_sha, &side);
    let err = check_push(fx.path(), &tip, &heads).unwrap_err();
    assert!(
        err.contains("is not new first-parent history of main"),
        "{err}"
    );
    // A commit that never landed is refused the same way.
    let stray = fx.commit(&fx.base(), "stray.txt", "x\n");
    let tip = remerge(fx.path(), &m.train_sha, &stray);
    assert!(check_push(fx.path(), &tip, &heads).is_err());
    // Main's own old history adds nothing: the train already contains it.
    let tree = format!("{}^{{tree}}", m.train_sha);
    let tip = g(
        fx.path(),
        &[
            "commit-tree",
            &tree,
            "-p",
            &m.train_sha,
            "-p",
            &fx.base(),
            "-m",
            "no-op",
        ],
    );
    assert!(check_push(fx.path(), &tip, &heads).is_err());
}

#[test]
fn an_unattributed_commit_after_a_remerge_and_a_rewritten_chain_are_still_refused() {
    let fx = Fx::new();
    let (m, _, heads) = open_train(&fx);
    let landed = land_other(&fx);
    let tip = remerge(fx.path(), &m.train_sha, &landed);
    let extra = fx.commit(&tip, "sneak.txt", "x\n");
    let err = check_push(fx.path(), &extra, &heads).unwrap_err();
    assert!(
        err.contains("extra commit without resolver attribution"),
        "{err}"
    );
    // A train that dropped a part's merge by resetting to the base has no merge for it.
    let err = check_push(fx.path(), &landed, &heads).unwrap_err();
    assert!(err.contains("0 merges"), "{err}");
}

#[test]
fn replay_records_the_remerge_for_the_diff_review_and_refuses_an_edited_one() {
    let fx = Fx::new();
    let (mut m, parts, _) = open_train(&fx);
    let landed = land_other(&fx);
    m.train_sha = remerge(fx.path(), &m.train_sha, &landed);
    let delta = verify_manifest_with(fx.path(), &m, &parts, &[], &[]).unwrap();
    assert_eq!(
        delta.main_in, landed,
        "the replay names the main the train contains"
    );
    let json = serde_json::to_value(&delta).unwrap();
    assert_eq!(json["remerges"].as_array().unwrap().len(), 1);
    assert_eq!(json["remerges"][0]["parents"][1], landed);
    assert_eq!(json["remerges"][0]["clean"], true);
    // An edit hidden in the remerge commit has no replay behind it.
    let tip = {
        let r = fx.path();
        g(r, &["checkout", "-q", "--detach", &m.merges[1]]);
        g(r, &["merge", "--no-ff", "--no-commit", "-q", &landed]);
        std::fs::write(r.join("hidden.txt"), "evil\n").unwrap();
        g(r, &["add", "-A"]);
        g(r, &["commit", "-q", "-m", "Merge main into train"]);
        let head = g(r, &["rev-parse", "HEAD"]);
        g(r, &["checkout", "-q", "main"]);
        head
    };
    m.train_sha = tip;
    let err = verify_manifest_with(fx.path(), &m, &parts, &[], &[]).unwrap_err();
    assert!(err.contains("extra or missing edit"), "{err}");
}

#[cfg(unix)]
#[test]
fn the_hook_form_with_a_relative_repository_path_replays_a_remerge() {
    let fx = Fx::new();
    let (m, _, heads) = open_train(&fx);
    let landed = land_other(&fx);
    let tip = remerge(fx.path(), &m.train_sha, &landed);
    // The installed hook runs `check-chain --repo .`; any relative path must name the same repository.
    let cwd = std::env::current_dir().unwrap();
    let up = "../".repeat(cwd.components().count() - 1);
    let relative = format!("{up}{}", fx.path().strip_prefix("/").unwrap().display());
    let args = [
        "check-chain",
        "--repo",
        &relative,
        "--head",
        &tip,
        "--heads",
        heads.to_str().unwrap(),
    ]
    .map(str::to_owned);
    let python = || std::process::Command::new("false");
    super::cli::run_inner(fx.path(), &args, &python).unwrap();
}

#[test]
fn a_part_that_brings_in_main_without_a_remerge_is_refused_even_with_a_hollow_merge() {
    let fx = Fx::new();
    let m0 = fx.base();
    g(fx.path(), &["update-ref", "refs/remotes/origin/main", &m0]);
    let a = fx.commit(&m0, "a.txt", "a\n");
    let landed = land_other(&fx);
    // A part branched from the new main carries its history into the train.
    let p = fx.commit(&landed, "p.txt", "p\n");
    let heads = fx.path().join(".git").join("heads.txt");
    std::fs::write(&heads, format!("{a}\n{p}\n")).unwrap();
    let r = fx.path();
    g(r, &["checkout", "-q", "--detach", &m0]);
    g(r, &["merge", "--no-ff", "-q", "-m", "part a", &a]);
    let first = g(r, &["rev-parse", "HEAD"]);
    g(r, &["merge", "--no-ff", "-q", "-m", "part p", &p]);
    let honest = g(r, &["rev-parse", "HEAD"]);
    g(r, &["checkout", "-q", "main"]);
    let err = check_push(r, &honest, &heads).unwrap_err();
    assert!(err.contains("no replayed remerge accounts for"), "{err}");
    // The same parents with a tree that leaves the landed file out: refused as well.
    let tree = format!("{first}^{{tree}}");
    let hollow = g(
        r,
        &["commit-tree", &tree, "-p", &first, "-p", &p, "-m", "part p"],
    );
    assert!(check_push(r, &hollow, &heads).is_err());
}
