//! Remote lane branches as lanes: whole-token slug matching against real repositories.

use super::*;

fn git(dir: &Path, args: &[&str]) {
    let out = Command::new("git")
        .arg("-C")
        .arg(dir)
        .args(["-c", "user.name=t", "-c", "user.email=t@example.test"])
        .args(args)
        .output()
        .unwrap();
    assert!(out.status.success(), "{args:?}: {out:?}");
}

#[test]
fn unmerged_remote_lane_branches_hold_the_key_slug() {
    let dir = tempfile::tempdir().unwrap();
    let repo = dir.path();
    git(repo, &["init", "-q", "-b", "main"]);
    git(repo, &["commit", "-q", "--allow-empty", "-m", "base"]);
    git(repo, &["update-ref", "refs/remotes/origin/main", "HEAD"]);
    git(repo, &["checkout", "-q", "-b", "lane/own-slug"]);
    git(repo, &["commit", "-q", "--allow-empty", "-m", "own"]);
    git(
        repo,
        &["update-ref", "refs/remotes/origin/lane/own-slug", "HEAD"],
    );
    git(
        repo,
        &["update-ref", "refs/remotes/origin/lane/fix-alpha-x", "HEAD"],
    );
    git(
        repo,
        &["update-ref", "refs/remotes/origin/lane/merged-beta", "main"],
    );
    let keys = [
        "demo:ALPHA-X".to_owned(),
        "demo:BETA".into(),
        "demo:OWN-SLUG".into(),
    ];
    let lanes = branch_lanes(repo, &keys);
    assert_eq!(
        lanes,
        [Lane {
            name: "origin/lane/fix-alpha-x".into(),
            keys: vec!["demo:ALPHA-X".into()],
            source: "branch",
        }]
    );
}

#[test]
fn branch_slug_matches_whole_tokens_only() {
    let dir = tempfile::tempdir().unwrap();
    let repo = dir.path();
    git(repo, &["init", "-q", "-b", "main"]);
    git(repo, &["commit", "-q", "--allow-empty", "-m", "base"]);
    git(repo, &["update-ref", "refs/remotes/origin/main", "HEAD"]);
    git(repo, &["checkout", "-q", "-b", "lane/mine"]);
    git(repo, &["commit", "-q", "--allow-empty", "-m", "own"]);
    for branch in [
        "lane/fixture-a",
        "lane/prefix-1",
        "lane/fix-b",
        "lane/a.fix_c",
    ] {
        git(
            repo,
            &[
                "update-ref",
                &format!("refs/remotes/origin/{branch}"),
                "HEAD",
            ],
        );
    }
    let lanes = branch_lanes(repo, &["demo:FIX".to_owned()]);
    let mut names: Vec<_> = lanes.iter().map(|lane| lane.name.as_str()).collect();
    names.sort_unstable();
    assert_eq!(names, ["origin/lane/a.fix_c", "origin/lane/fix-b"]);
    // A slug of several tokens must appear in order and side by side.
    let keys = ["demo:ALPHA-X".to_owned()];
    git(
        repo,
        &["update-ref", "refs/remotes/origin/lane/x-alpha", "HEAD"],
    );
    assert!(branch_lanes(repo, &keys).is_empty());
}

#[test]
fn the_remote_name_is_not_part_of_a_branch_slug() {
    let dir = tempfile::tempdir().unwrap();
    let repo = dir.path();
    git(repo, &["init", "-q", "-b", "main"]);
    git(repo, &["commit", "-q", "--allow-empty", "-m", "base"]);
    git(repo, &["update-ref", "refs/remotes/origin/main", "HEAD"]);
    git(repo, &["checkout", "-q", "-b", "lane/mine"]);
    git(repo, &["commit", "-q", "--allow-empty", "-m", "own"]);
    for branch in ["lane/unrelated", "lane/origin-fix"] {
        git(
            repo,
            &[
                "update-ref",
                &format!("refs/remotes/origin/{branch}"),
                "HEAD",
            ],
        );
    }
    let lanes = branch_lanes(repo, &["demo:ORIGIN".to_owned()]);
    let names: Vec<_> = lanes.iter().map(|lane| lane.name.as_str()).collect();
    assert_eq!(names, ["origin/lane/origin-fix"]);
}

#[test]
fn a_branch_holds_a_key_with_a_second_colon_by_all_its_id_tokens() {
    let dir = tempfile::tempdir().unwrap();
    let repo = dir.path();
    git(repo, &["init", "-q", "-b", "main"]);
    git(repo, &["commit", "-q", "--allow-empty", "-m", "base"]);
    git(repo, &["update-ref", "refs/remotes/origin/main", "HEAD"]);
    git(repo, &["checkout", "-q", "-b", "lane/mine"]);
    git(repo, &["commit", "-q", "--allow-empty", "-m", "own"]);
    for branch in ["lane/item-detail", "lane/detail-only"] {
        git(
            repo,
            &[
                "update-ref",
                &format!("refs/remotes/origin/{branch}"),
                "HEAD",
            ],
        );
    }
    let lanes = branch_lanes(repo, &["demo:ITEM:detail".to_owned()]);
    let names: Vec<_> = lanes.iter().map(|lane| lane.name.as_str()).collect();
    assert_eq!(names, ["origin/lane/item-detail"]);
}
