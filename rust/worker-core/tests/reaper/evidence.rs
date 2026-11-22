use super::*;
use pg_worker_core::reaper::reap_with_authority;

fn options(f: &Fixture) -> Options<'_> {
    Options {
        repo: &f.repo,
        roots: std::slice::from_ref(&f.root),
        targets: &[],
        remote_targets: &[],
        idle_minutes: 60,
        state: &f.state,
        pins: &f.pins,
        claims_reader: None,
        apply: false,
        include_merged: false,
        delete_remote: false,
    }
}

fn terminal_run(f: &Fixture, name: &str, head: &str) -> PathBuf {
    let run = f.state.join("runs").join(name);
    fs::create_dir_all(&run).unwrap();
    let record = run.join("record.json");
    fs::write(
        &record,
        serde_json::json!({
            "status":"completed", "cwd":run.join("worktree"), "repo":f.repo, "final_head":head,
        })
        .to_string(),
    )
    .unwrap();
    for path in [&run, &record] {
        fs::File::open(path)
            .unwrap()
            .set_times(
                fs::FileTimes::new()
                    .set_modified(SystemTime::now() - Duration::from_secs(8 * 86_400)),
            )
            .unwrap();
    }
    run
}

fn plan_for(report: &str, action: &str, path: &Path) -> String {
    report
        .lines()
        .filter_map(|line| serde_json::from_str::<serde_json::Value>(line).ok())
        .find(|row| row["action"] == action && row["path"] == path.to_str().unwrap())
        .unwrap_or_else(|| panic!("missing {action} for {path:?}: {report}"))
        .to_string()
}

fn mature_slot(f: &Fixture) -> PathBuf {
    let slot = fs::read_dir(f.state.join("reap-quarantine"))
        .unwrap()
        .next()
        .unwrap()
        .unwrap()
        .path();
    let receipt = slot.join("receipt.json");
    let mut data: serde_json::Value = serde_json::from_slice(&fs::read(&receipt).unwrap()).unwrap();
    let now = SystemTime::now()
        .duration_since(SystemTime::UNIX_EPOCH)
        .unwrap()
        .as_secs();
    data["quarantined_at"] = (now - 8 * 86_400).into();
    fs::write(&receipt, data.to_string()).unwrap();
    slot
}

#[test]
fn a_claim_appearing_after_the_creation_lock_protects_worktrees_and_run_dirs() {
    for run_dir in [false, true] {
        let f = Fixture::new();
        let path = if run_dir {
            terminal_run(&f, "run-late-claim", &f.sha)
        } else {
            let tree = f.root.join("topic");
            age(&tree, 8 * 86_400);
            tree
        };
        let opts = options(&f);
        let report = reap_with_authority(&opts, None, authority).unwrap();
        let plan = plan_for(&report, "would-quarantine", &path);
        let lock = f.repo.join(".git/plangraph-worktree.lock");
        let mut reads = 0;
        let outcome = reap_with_authority(&opts, Some(("D-123", &plan)), |opts, branch| {
            reads += 1;
            let (mut protection, pulls) = authority(opts, branch)?;
            if reads == 2 {
                assert!(pg_worker_core::lockfile::try_lock(&lock).unwrap().is_none());
                protection.claims.paths.push(path.clone());
            }
            Ok((protection, pulls))
        })
        .unwrap();
        assert!(path.exists(), "{outcome}");
        assert_eq!(reads, 2);
        assert!(
            outcome.contains(if run_dir {
                "run directory is in use"
            } else {
                ": claim"
            }),
            "{outcome}"
        );
        assert!(!f.state.join("reap-quarantine").exists());
    }
}

#[test]
fn an_open_pull_or_process_inside_a_candidate_protects_report_and_approved_actions() {
    let f = Fixture::new();
    let tree = f.root.join("topic");
    age(&tree, 8 * 86_400);
    let opts = options(&f);
    let report = reap_with_authority(&opts, None, authority).unwrap();
    let stage = plan_for(&report, "would-quarantine", &tree);
    let protect = |opts: &Options<'_>, branch: &str, path: &Path, open_pr: bool| {
        let (mut protection, mut pulls) = authority(opts, branch)?;
        if open_pr {
            pulls.push(Pull {
                number: 7,
                state: "OPEN".into(),
                sha: f.sha.clone(),
            });
        } else {
            protection.cwds.push(path.join("tracked-subdirectory"));
        }
        Ok((protection, pulls))
    };
    let cases = [(true, "open-pr"), (false, "process-in-use")];
    for (open_pr, reason) in cases {
        for approval in [None, Some(("D-123", stage.as_str()))] {
            let out =
                reap_with_authority(&opts, approval, |o, b| protect(o, b, &tree, open_pr)).unwrap();
            assert!(
                out.contains(reason) && !out.contains("would-quarantine"),
                "{out}"
            );
            assert!(tree.exists() && !f.state.join("reap-quarantine").exists());
        }
    }

    reap_with_authority(&opts, Some(("D-123", &stage)), authority).unwrap();
    let slot = mature_slot(&f);

    let staged = slot.join("worktree");
    let report = reap_with_authority(&opts, None, authority).unwrap();
    let deletion = plan_for(&report, "would-delete", &staged);
    for (open_pr, reason) in cases {
        let out = reap_with_authority(&opts, Some(("D-124", &deletion)), |o, b| {
            protect(o, b, &staged, open_pr)
        })
        .unwrap();
        assert!(out.contains(reason) && staged.exists(), "{out}");
        assert!(!out.contains("delete-intent"));
    }
}

#[test]
fn report_reuses_protection_branch_pulls_and_the_open_inventory_for_detached_heads() {
    use pg_worker_core::reaper::reap_with_readers;
    let f = Fixture::new();
    let tree = f.root.join("topic");
    age(&tree, 8 * 86_400);
    git(
        &f.repo,
        &["commit", "--allow-empty", "-qm", "another landed head"],
    );
    git(&f.repo, &["push", "-q", "origin", "main"]);
    let newer = git(&f.repo, &["rev-parse", "HEAD"]);
    let blocked = terminal_run(&f, "run-detached-open", &f.sha);
    let available = terminal_run(&f, "run-detached-available", newer.trim());
    let named = terminal_run(&f, "run-named", &f.sha);
    let record = named.join("record.json");
    let mut data: serde_json::Value = serde_json::from_slice(&fs::read(&record).unwrap()).unwrap();
    data["lane_branch"] = "topic".into();
    fs::write(&record, data.to_string()).unwrap();
    fs::File::open(record)
        .unwrap()
        .set_times(
            fs::FileTimes::new().set_modified(SystemTime::now() - Duration::from_secs(8 * 86_400)),
        )
        .unwrap();
    let opts = options(&f);
    let mut protection_reads = 0;
    let mut pull_queries = Vec::new();
    let report = reap_with_readers(
        &opts,
        None,
        |o| {
            protection_reads += 1;
            authority(o, "").map(|(protection, _)| protection)
        },
        |_, branch| {
            pull_queries.push(branch.map(str::to_owned));
            Ok(if branch.is_none() {
                vec![Pull {
                    number: 7,
                    state: "OPEN".into(),
                    sha: f.sha.clone(),
                }]
            } else {
                vec![]
            })
        },
    )
    .unwrap();
    plan_for(&report, "would-quarantine", &tree);
    plan_for(&report, "would-quarantine", &available);
    plan_for(&report, "would-quarantine", &named);
    assert!(
        report.contains(&format!("{}: open PR", blocked.display())),
        "{report}"
    );
    assert_eq!(protection_reads, 1);
    assert_eq!(pull_queries, [Some("topic".into()), None]);
}

#[test]
fn attempt_creation_waits_for_the_reaper_lock() {
    use pg_worker_core::worktree::{create, mutation_lock, AttemptId};
    let f = Fixture::new();
    let tree = f.root.join("new-attempt");
    let held = mutation_lock(&f.repo).unwrap();
    let error = create(&f.repo, &f.sha, &tree, &AttemptId::new("fixture", 1)).unwrap_err();
    assert!(
        error
            .to_string()
            .contains("lock not acquired before the deadline"),
        "{error}"
    );
    assert!(!tree.exists());
    drop(held);
    create(&f.repo, &f.sha, &tree, &AttemptId::new("fixture", 1)).unwrap();
}

#[test]
fn approved_run_directory_deletion_leaves_creation_unlocked() {
    let f = Fixture::new();
    let run = terminal_run(&f, "run-delete", &f.sha);
    let opts = options(&f);
    let report = reap_with_authority(&opts, None, authority).unwrap();
    let stage = plan_for(&report, "would-quarantine", &run);
    reap_with_authority(&opts, Some(("D-123", &stage)), authority).unwrap();
    let slot = mature_slot(&f);
    let staged = slot.join("run-dir");
    let report = reap_with_authority(&opts, None, authority).unwrap();
    let deletion = plan_for(&report, "would-delete", &staged);
    let mut reads = 0;
    let lock = f.repo.join(".git/plangraph-worktree.lock");
    let report = reap_with_authority(&opts, Some(("D-124", &deletion)), |o, b| {
        reads += 1;
        if reads == 2 {
            assert!(pg_worker_core::lockfile::try_lock(&lock).unwrap().is_some());
        }
        authority(o, b)
    })
    .unwrap();
    assert_eq!(reads, 2);
    assert!(report.contains("deleted") && !staged.exists(), "{report}");
}
