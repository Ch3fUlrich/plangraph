#![cfg(unix)]
use pg_worker_core::{
    disk_hygiene::Guards,
    merged_sweep::{run_with_authority, Options},
    sweep_facts::{Claims, Protection, Pull},
};
use std::{
    fs,
    path::{Path, PathBuf},
    process::Command,
    time::{Duration, SystemTime},
};
fn git(repo: &Path, args: &[&str]) -> String {
    let out = pg_worker_core::git_test_support::command(repo)
        .args([
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
        ])
        .args(args)
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{args:?}: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    String::from_utf8(out.stdout).unwrap()
}
struct Fixture {
    _tmp: tempfile::TempDir,
    repo: PathBuf,
    root: PathBuf,
    state: PathBuf,
    sha: String,
}
impl Fixture {
    fn new() -> Self {
        let tmp = tempfile::tempdir().unwrap();
        let repo = tmp.path().join("repo");
        let root = tmp.path().join("trees");
        let state = tmp.path().join("state");
        for path in [&repo, &root, &state] {
            fs::create_dir(path).unwrap();
        }
        git(&repo, &["init", "-qb", "main"]);
        fs::write(repo.join("one"), "one").unwrap();
        git(&repo, &["add", "."]);
        git(&repo, &["commit", "-qm", "initial"]);
        let remote = tmp.path().join("remote");
        fs::create_dir(&remote).unwrap();
        git(&remote, &["init", "--bare", "-q"]);
        git(
            &repo,
            &["remote", "add", "origin", remote.to_str().unwrap()],
        );
        git(&repo, &["push", "-q", "origin", "main"]);
        git(
            &repo,
            &[
                "worktree",
                "add",
                "-qb",
                "topic",
                root.join("topic").to_str().unwrap(),
            ],
        );
        git(&repo, &["push", "-q", "origin", "topic"]);
        let tree_git = git(&root.join("topic"), &["rev-parse", "--absolute-git-dir"]);
        for name in ["HEAD", "index"] {
            fs::File::options()
                .write(true)
                .open(Path::new(tree_git.trim()).join(name))
                .unwrap()
                .set_times(
                    fs::FileTimes::new()
                        .set_modified(SystemTime::now() - Duration::from_secs(90000)),
                )
                .unwrap();
        }
        fs::write(state.join("pins.json"), "[]").unwrap();
        let sha = git(&repo, &["rev-parse", "HEAD"]).trim().to_string();
        Self {
            _tmp: tmp,
            repo,
            root,
            state,
            sha,
        }
    }
    fn command(&self) -> Command {
        let mut command = Command::new(env!("CARGO_BIN_EXE_pg-worktree-sweep"));
        command
            .current_dir(&self.repo)
            .arg("--repo")
            .arg(&self.repo)
            .arg("--root")
            .arg(&self.root)
            .arg("--state")
            .arg(&self.state)
            .arg("--pins")
            .arg(self.state.join("pins.json"))
            .env_remove("PG_SWEEP_CLAIMS_READER");
        command
    }
}
#[test]
fn sweep_cli_dry_run_then_apply_saves_restore_and_keeps_remote() {
    let f = Fixture::new();
    let execute = |apply| {
        run_with_authority(
            &Options {
                repo: &f.repo,
                roots: std::slice::from_ref(&f.root),
                targets: &[],
                remote_targets: &[],
                idle_minutes: 60,
                state: &f.state,
                pins: &f.state.join("pins.json"),
                claims_reader: None,
                apply,
                delete_remote: false,
            },
            |_, _| {
                Ok((
                    Protection {
                        guards: Guards::default(),
                        claims: Claims {
                            paths: vec![],
                            branches: vec![],
                        },
                        units: vec![],
                        unit_refs: vec![],
                        cwds: vec![],
                    },
                    vec![Pull {
                        number: 7,
                        state: "MERGED".into(),
                        sha: f.sha.clone(),
                    }],
                ))
            },
        )
        .unwrap()
    };
    let out = execute(false);
    assert!(out.contains("would-remove"), "{out}");
    assert!(f.root.join("topic").exists());
    assert!(!f.state.join("sweep").exists());
    let out = execute(true);
    assert!(out.contains("removed"), "{out}");
    assert!(!f.root.join("topic").exists());
    let restore = fs::read_dir(f.state.join("sweep"))
        .unwrap()
        .next()
        .unwrap()
        .unwrap()
        .path();
    let rows = fs::read_to_string(restore).unwrap();
    assert!(rows.starts_with("repo\tpath\tbranch\tsha\tPR\n"));
    assert!(rows.contains(&format!("\trefs/heads/topic\t{}\t7\n", f.sha)));
    assert!(git(&f.repo, &["branch", "--list", "topic"])
        .trim()
        .is_empty());
    assert!(git(&f.repo, &["ls-remote", "--heads", "origin", "topic"]).contains(&f.sha));
}
#[test]
fn sweep_cli_missing_claim_authority_protects_and_daily_marker_skips() {
    let f = Fixture::new();
    let out = f.command().arg("--apply").output().unwrap();
    assert!(out.status.success());
    assert!(f.root.join("topic").exists());
    assert!(String::from_utf8_lossy(&out.stdout).contains("claims reader unavailable"));
    fs::write(f.state.join("merged-sweep.last"), "completed").unwrap();
    let out = f.command().args(["--daily", "--apply"]).output().unwrap();
    assert!(out.status.success());
    assert!(out.stdout.is_empty());
    assert!(f.root.join("topic").exists());
}
#[test]
fn sweep_apply_restore_failure_prevents_removal_and_remote_requires_merged_pr() {
    for state in ["CLOSED", "MERGED"] {
        let f = Fixture::new();
        let opts = Options {
            repo: &f.repo,
            roots: std::slice::from_ref(&f.root),
            targets: &[],
            remote_targets: &[],
            idle_minutes: 60,
            state: &f.state,
            pins: &f.state.join("pins.json"),
            claims_reader: None,
            apply: true,
            delete_remote: true,
        };
        let mut authority = |_: &Options<'_>, _: &str| {
            Ok((
                Protection {
                    guards: Guards::default(),
                    claims: Claims {
                        paths: vec![],
                        branches: vec![],
                    },
                    units: vec![],
                    unit_refs: vec![],
                    cwds: vec![],
                },
                vec![Pull {
                    number: 7,
                    state: state.into(),
                    sha: f.sha.clone(),
                }],
            ))
        };
        fs::write(f.state.join("sweep"), "not a directory").unwrap();
        assert!(run_with_authority(&opts, &mut authority).is_err());
        assert!(f.root.join("topic").exists());
        fs::remove_file(f.state.join("sweep")).unwrap();
        let report = run_with_authority(&opts, &mut authority).unwrap();
        assert!(report.contains("removed"), "{report}");
        assert!(!f.root.join("topic").exists());
        let restore = fs::read_dir(f.state.join("sweep"))
            .unwrap()
            .next()
            .unwrap()
            .unwrap()
            .path();
        assert!(fs::read_to_string(&restore)
            .unwrap()
            .contains(&format!("\t{}\t7\n", f.sha)));
        if state == "MERGED" {
            assert!(fs::read_to_string(&restore)
                .unwrap()
                .contains(&format!("\t-\trefs/heads/topic\t{}\t-\n", f.sha)));
        }
        let remote = git(&f.repo, &["ls-remote", "--heads", "origin", "topic"]);
        assert_eq!(remote.is_empty(), state == "MERGED");
    }
}

fn authority(options: &Options<'_>, _: &str) -> std::io::Result<(Protection, Vec<Pull>)> {
    Ok((
        Protection {
            guards: Guards::load(options.pins, options.state)?,
            claims: Claims {
                paths: vec![],
                branches: vec![],
            },
            units: vec![],
            unit_refs: vec![],
            cwds: vec![],
        },
        vec![],
    ))
}
fn age(tree: &Path, seconds: u64) {
    let dir = git(tree, &["rev-parse", "--absolute-git-dir"]);
    for name in ["HEAD", "index"] {
        fs::File::options()
            .write(true)
            .open(Path::new(dir.trim()).join(name))
            .unwrap()
            .set_times(
                fs::FileTimes::new().set_modified(SystemTime::now() - Duration::from_secs(seconds)),
            )
            .unwrap();
    }
}
#[test]
fn detached_terminal_runs_multiple_roots_and_idle_threshold() {
    let f = Fixture::new();
    let runs = f.state.join("runs");
    fs::create_dir_all(runs.join("done")).unwrap();
    fs::create_dir_all(runs.join("active")).unwrap();
    let done = runs.join("done/worktree");
    let active = runs.join("active/worktree");
    for tree in [&done, &active] {
        git(
            &f.repo,
            &[
                "worktree",
                "add",
                "--detach",
                tree.to_str().unwrap(),
                "main",
            ],
        );
        age(tree, 7200);
    }
    fs::write(
        runs.join("done/record.json"),
        serde_json::json!({"status":"completed", "checkout":done}).to_string(),
    )
    .unwrap();
    fs::write(
        runs.join("active/record.json"),
        serde_json::json!({"status":"running", "checkout":active}).to_string(),
    )
    .unwrap();
    let other_root = f._tmp.path().join("other-trees");
    fs::create_dir(&other_root).unwrap();
    let other = other_root.join("detached");
    git(
        &f.repo,
        &[
            "worktree",
            "add",
            "--detach",
            other.to_str().unwrap(),
            "main",
        ],
    );
    age(&other, 7200);
    // Two independent explicit roots, plus automatic discovery of state/runs.
    let roots = [f.root.clone(), other_root];
    let opts = Options {
        repo: &f.repo,
        roots: &roots,
        remote_targets: &[],
        targets: &[],
        idle_minutes: 180,
        state: &f.state,
        pins: &f.state.join("pins.json"),
        claims_reader: None,
        apply: true,
        delete_remote: false,
    };
    let out = run_with_authority(&opts, authority).unwrap();
    assert!(out.contains("idle-below-clean-threshold"), "{out}");
    assert!(done.exists() && active.exists() && other.exists());
    let opts = Options {
        idle_minutes: 60,
        ..opts
    };
    let out = run_with_authority(&opts, authority).unwrap();
    assert!(out.contains("nonterminal run"), "{out}");
    assert!(!done.exists() && active.exists() && !other.exists() && !f.root.join("topic").exists());
}
#[test]
fn detached_unmerged_and_unpushed_commits_and_failed_claims_survive() {
    let f = Fixture::new();
    let tree = f.root.join("topic");
    git(&tree, &["checkout", "--detach"]);
    git(&tree, &["commit", "--allow-empty", "-qm", "unmerged"]);
    age(&tree, 90000);
    let opts = Options {
        repo: &f.repo,
        roots: std::slice::from_ref(&f.root),
        remote_targets: &[],
        targets: &[],
        idle_minutes: 60,
        state: &f.state,
        pins: &f.state.join("pins.json"),
        claims_reader: None,
        apply: true,
        delete_remote: false,
    };
    let out = run_with_authority(&opts, authority).unwrap();
    assert!(out.contains("unmerged-commits") && tree.exists(), "{out}");
    git(&tree, &["checkout", "--detach", "origin/main"]);
    age(&tree, 90000);
    let out =
        run_with_authority(&opts, |_, _| Err(std::io::Error::other("claims failure"))).unwrap();
    assert!(out.contains("claims failure") && tree.exists(), "{out}");
    // A merged detached tree is eligible and removal handles symbolic HEAD safely.
    let out = run_with_authority(&opts, authority).unwrap();
    assert!(out.contains("removed") && !tree.exists(), "{out}");
}

#[test]
fn exact_targets_remote_leases_and_claim_failure_are_safe() {
    for failure in [false, true] {
        let f = Fixture::new();
        let tree = f.root.join("topic");
        let targets = [format!("{}:refs/heads/topic:{}", tree.display(), f.sha)];
        let remotes = [format!("topic:{}", f.sha)];
        let opts = Options {
            repo: &f.repo,
            roots: std::slice::from_ref(&f.root),
            remote_targets: &remotes,
            targets: &targets,
            idle_minutes: 60,
            state: &f.state,
            pins: &f.state.join("pins.json"),
            claims_reader: None,
            apply: true,
            delete_remote: true,
        };
        let out = run_with_authority(&opts, |o, b| {
            if failure {
                Err(std::io::Error::other("claims reader failed"))
            } else {
                authority(o, b)
            }
        })
        .unwrap();
        assert_eq!(tree.exists(), failure, "{out}");
        assert_eq!(
            git(&f.repo, &["ls-remote", "--heads", "origin", "topic"]).is_empty(),
            !failure,
            "{out}"
        );
        let restored: String = fs::read_dir(f.state.join("sweep"))
            .unwrap()
            .map(|e| fs::read_to_string(e.unwrap().path()).unwrap())
            .collect();
        assert_eq!(restored.contains(&f.sha), !failure);
    }
    let f = Fixture::new();
    let targets = [format!(
        "{}:refs/heads/topic:{}",
        f.root.join("topic").display(),
        "f".repeat(40)
    )];
    let opts = Options {
        repo: &f.repo,
        roots: std::slice::from_ref(&f.root),
        remote_targets: &[],
        targets: &targets,
        idle_minutes: 60,
        state: &f.state,
        pins: &f.state.join("pins.json"),
        claims_reader: None,
        apply: true,
        delete_remote: false,
    };
    assert!(run_with_authority(&opts, authority)
        .unwrap()
        .contains("outside exact targets"));
    assert!(f.root.join("topic").exists());
}

#[test]
fn relative_state_still_protects_running_run_worktrees() {
    let f = Fixture::new();
    let run = f.state.join("runs/active");
    fs::create_dir_all(&run).unwrap();
    let tree = run.join("worktree");
    git(
        &f.repo,
        &[
            "worktree",
            "add",
            "--detach",
            tree.to_str().unwrap(),
            "main",
        ],
    );
    age(&tree, 90000);
    fs::write(
        run.join("record.json"),
        serde_json::json!({"status":"running", "cwd":f.repo}).to_string(),
    )
    .unwrap();
    let cwd = std::env::current_dir().unwrap();
    let mut relative = PathBuf::from("../".repeat(cwd.components().count() - 1));
    relative.push(f.state.strip_prefix("/").unwrap());
    assert!(!relative.is_absolute());
    let opts = Options {
        repo: &f.repo,
        roots: std::slice::from_ref(&f.root),
        remote_targets: &[],
        targets: &[],
        idle_minutes: 60,
        state: &relative,
        pins: &f.state.join("pins.json"),
        claims_reader: None,
        apply: true,
        delete_remote: false,
    };
    let out = run_with_authority(&opts, authority).unwrap();
    assert!(tree.exists() && out.contains("nonterminal run"), "{out}");
}

#[test]
fn exact_targets_remove_only_selected_tree_and_missing_optional_root_is_safe() {
    let f = Fixture::new();
    let selected = f.root.join("topic");
    let other = f.root.join("unrelated");
    git(
        &f.repo,
        &[
            "worktree",
            "add",
            "-qb",
            "unrelated",
            other.to_str().unwrap(),
        ],
    );
    age(&other, 90000);
    let roots = [f.root.clone(), f._tmp.path().join("absent-optional-root")];
    let targets = [format!("{}:refs/heads/topic:{}", selected.display(), f.sha)];
    let opts = Options {
        repo: &f.repo,
        roots: &roots,
        remote_targets: &[],
        targets: &targets,
        idle_minutes: 60,
        state: &f.state,
        pins: &f.state.join("pins.json"),
        claims_reader: None,
        apply: true,
        delete_remote: false,
    };
    let out = run_with_authority(&opts, authority).unwrap();
    assert!(!selected.exists() && other.exists(), "{out}");
    assert!(
        out.contains("root not found") && out.contains("outside exact targets"),
        "{out}"
    );
    assert!(git(&f.repo, &["branch", "--list", "unrelated"]).contains("unrelated"));
}

#[test]
fn native_terminals_and_verified_legacy_aliases_work_and_unknown_statuses_protect() {
    let terminal = [
        "completed",
        "failed",
        "timed_out",
        "cancelled",
        "refused",
        "stalled",
        "timeout",
        "dry_run",
        "killed",
        "succeeded",
        "skipped",
        "terminated_after_answer",
    ];
    for status in terminal.iter().chain(
        [
            "running",
            "starting",
            "queued",
            "unknown-status",
            "unreadable",
        ]
        .iter(),
    ) {
        let f = Fixture::new();
        let run = f.state.join("runs/one");
        fs::create_dir_all(&run).unwrap();
        let tree = run.join("worktree");
        git(
            &f.repo,
            &[
                "worktree",
                "add",
                "--detach",
                tree.to_str().unwrap(),
                "main",
            ],
        );
        age(&tree, 90000);
        fs::write(
            run.join("record.json"),
            if *status == "unreadable" {
                "broken".into()
            } else {
                serde_json::json!({"status":status,"checkout":tree}).to_string()
            },
        )
        .unwrap();
        let opts = Options {
            repo: &f.repo,
            roots: std::slice::from_ref(&f.root),
            remote_targets: &[],
            targets: &[],
            idle_minutes: 60,
            state: &f.state,
            pins: &f.state.join("pins.json"),
            claims_reader: None,
            apply: true,
            delete_remote: false,
        };
        let out = run_with_authority(&opts, authority).unwrap();
        assert_eq!(!tree.exists(), terminal.contains(status), "{status}: {out}");
    }
}

#[test]
fn protected_branch_names_survive_both_remote_deletion_modes() {
    for name in ["live-writer", "plangraph-md-sync"] {
        for explicit in [false, true] {
            let f = Fixture::new();
            git(&f.repo, &["branch", "-m", "topic", name]);
            git(&f.repo, &["push", "-q", "origin", name]);
            age(&f.root.join("topic"), 90000);
            let remotes = if explicit {
                vec![format!("{name}:{}", f.sha)]
            } else {
                vec![]
            };
            let opts = Options {
                repo: &f.repo,
                roots: std::slice::from_ref(&f.root),
                remote_targets: &remotes,
                targets: &[],
                idle_minutes: 60,
                state: &f.state,
                pins: &f.state.join("pins.json"),
                claims_reader: None,
                apply: true,
                delete_remote: true,
            };
            let out = run_with_authority(&opts, |_, _| {
                let (p, _) = authority(&opts, name)?;
                Ok((
                    p,
                    vec![Pull {
                        number: 1,
                        state: "MERGED".into(),
                        sha: f.sha.clone(),
                    }],
                ))
            })
            .unwrap();
            assert!(f.root.join("topic").exists(), "{out}");
            assert!(
                !git(&f.repo, &["ls-remote", "--heads", "origin", name]).is_empty(),
                "{out}"
            );
        }
    }
}

#[test]
fn legacy_attempts_require_a_matching_terminal_record() {
    for status in [
        "completed",
        "timeout",
        "dry_run",
        "stalled",
        "survivors",
        "unmerged",
        "unreadable",
        "running",
        "starting",
        "queued",
        "orphan",
    ] {
        let f = Fixture::new();
        let root = f.state.join("worktrees");
        fs::create_dir(&root).unwrap();
        let tree = root.join("attempt-fixture");
        git(
            &f.repo,
            &[
                "worktree",
                "add",
                "--detach",
                tree.to_str().unwrap(),
                "main",
            ],
        );
        if status == "unmerged" {
            git(&tree, &["commit", "--allow-empty", "-qm", "not on main"]);
        }
        age(&tree, 90000);
        let record = f.state.join("runs/legacy/record.json");
        fs::create_dir_all(record.parent().unwrap()).unwrap();
        let owner = if status == "orphan" {
            f.repo.clone()
        } else {
            tree.clone()
        };
        let record_status = if ["orphan", "survivors", "unmerged"].contains(&status) {
            "completed"
        } else {
            status
        };
        fs::write(record, serde_json::json!({"status":record_status, "cwd":owner,
            "run_worktree":{"path":owner,"branch":"lane/legacy"}, "worktree_attempts":[{"path":owner}],"survivors":if status == "survivors" {vec![42]} else {vec![]}}).to_string()).unwrap();
        if status == "unreadable" {
            fs::write(f.state.join("runs/legacy/record.json"), "broken").unwrap();
        }
        let opts = Options {
            repo: &f.repo,
            roots: std::slice::from_ref(&f.root),
            remote_targets: &[],
            targets: &[],
            idle_minutes: 60,
            state: &f.state,
            pins: &f.state.join("pins.json"),
            claims_reader: None,
            apply: true,
            delete_remote: false,
        };
        let out = run_with_authority(&opts, authority).unwrap();
        assert_eq!(
            !tree.exists(),
            ["completed", "timeout", "dry_run", "stalled"].contains(&status),
            "{status}: {out}"
        );
        if status == "unmerged" {
            assert!(out.contains("unmerged-commits"), "{out}");
        }
        if status == "survivors" {
            assert!(out.contains("nonterminal run"), "{out}");
        }
    }
}

#[test]
fn empty_root_configuration_fails_closed() {
    let f = Fixture::new();
    let out = f.command().args(["--root", ""]).output().unwrap();
    assert!(!out.status.success());
    assert!(String::from_utf8_lossy(&out.stderr).contains("empty path"));
    assert!(f.root.join("topic").exists());
}

#[test]
fn remote_sha_lease_preserves_an_advanced_unmerged_branch() {
    let f = Fixture::new();
    let advanced = git(
        &f.repo,
        &[
            "commit-tree",
            "HEAD^{tree}",
            "-p",
            "HEAD",
            "-m",
            "unmerged remote advance",
        ],
    );
    let advanced = advanced.trim();
    git(
        &f.repo,
        &[
            "push",
            "-q",
            "origin",
            &format!("{advanced}:refs/heads/topic"),
        ],
    );
    let remotes = [format!("topic:{}", f.sha)];
    let opts = Options {
        repo: &f.repo,
        roots: std::slice::from_ref(&f.root),
        remote_targets: &remotes,
        targets: &[],
        idle_minutes: 60,
        state: &f.state,
        pins: &f.state.join("pins.json"),
        claims_reader: None,
        apply: true,
        delete_remote: true,
    };
    let out = run_with_authority(&opts, authority).unwrap();
    assert!(!f.root.join("topic").exists(), "{out}");
    assert!(out.contains("protected remote"), "{out}");
    assert!(
        git(&f.repo, &["ls-remote", "--heads", "origin", "topic"]).contains(advanced),
        "{out}"
    );
}
