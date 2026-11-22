use super::{age, authority, git, run_with_authority, Fixture, Options, Pull};

#[test]
fn landing_sweep_keeps_protected_and_unmerged_worktrees_and_the_unrelated_remote() {
    let f = Fixture::new();
    let protected = f.root.join("protected");
    let unmerged = f.root.join("unmerged");
    for (path, branch) in [(&protected, "protected"), (&unmerged, "unmerged")] {
        git(
            &f.repo,
            &["worktree", "add", "-qb", branch, path.to_str().unwrap()],
        );
    }
    git(&f.repo, &["worktree", "lock", protected.to_str().unwrap()]);
    git(&unmerged, &["commit", "--allow-empty", "-qm", "unmerged"]);
    age(&protected, 90000);
    age(&unmerged, 90000);
    let options = Options {
        repo: &f.repo,
        roots: std::slice::from_ref(&f.root),
        remote_targets: &[],
        targets: &[format!(
            "{}:refs/heads/landed:{}",
            f.root.join("landed").display(),
            f.sha
        )],
        idle_minutes: 60,
        state: &f.state,
        pins: &f.state.join("pins.json"),
        claims_reader: None,
        apply: true,
        include_merged: true,
        delete_remote: true,
    };
    let out = run_with_authority(&options, |options, branch| {
        let (protection, _) = authority(options, branch)?;
        Ok((
            protection,
            vec![Pull {
                number: 7,
                state: "MERGED".into(),
                sha: f.sha.clone(),
            }],
        ))
    })
    .unwrap();
    assert!(
        !f.root.join("topic").exists() && protected.exists() && unmerged.exists(),
        "{out}"
    );
    assert!(git(&f.repo, &["ls-remote", "--heads", "origin", "topic"]).contains(&f.sha));
    assert_eq!(f.state.join("sweep").read_dir().unwrap().count(), 1);
}
