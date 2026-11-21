#![cfg(target_os = "linux")]
use pg_reap::{parse_porcelain, Facts};
use pg_worker_core::{
    disk_hygiene::Guards,
    merged_sweep::{eligible, remove, restore_row},
    sweep_facts::{facts, Claims, Protection, Pull},
};
use std::{
    fs,
    path::Path,
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
fn facts_fixture() -> Facts {
    serde_json::from_value(serde_json::json!({"idle_hours":24,"clean":true,"merged_into_main":true,"pushed":true,"failed":false,"has_claim":false,"live_run":false,"open_pr":false,"in_use_by_process":false,"in_use_by_unit":false,"is_clone_path":false})).unwrap()
}
#[test]
fn merged_sweep_uses_pg_reap_and_never_applies_dirty_backup_rule() {
    let trees = parse_porcelain("worktree /main\nHEAD abc\nbranch refs/heads/main\n\nworktree /tree\nHEAD def\nbranch refs/heads/topic\n").unwrap();
    let mut facts = facts_fixture();
    assert!(eligible(&trees[1], &facts, &trees));
    facts.clean = false;
    facts.idle_hours = 999;
    assert!(!eligible(&trees[1], &facts, &trees));
    for key in [
        "has_claim",
        "live_run",
        "open_pr",
        "in_use_by_process",
        "in_use_by_unit",
        "is_clone_path",
    ] {
        let mut row = serde_json::json!({"idle_hours":24,"clean":true,"merged_into_main":true,"pushed":true,"failed":false,"has_claim":false,"live_run":false,"open_pr":false,"in_use_by_process":false,"in_use_by_unit":false,"is_clone_path":false});
        row[key] = true.into();
        assert!(
            !eligible(&trees[1], &serde_json::from_value(row).unwrap(), &trees),
            "{key}"
        );
    }
    facts = facts_fixture();
    facts.pushed = false;
    assert!(!eligible(&trees[1], &facts, &trees));
    facts = facts_fixture();
    facts.idle_hours = 23;
    assert!(!eligible(&trees[1], &facts, &trees));
    assert!(!eligible(&trees[0], &facts_fixture(), &trees));
}
#[test]
fn merged_sweep_nested_merge_live_facts_and_safe_removal() {
    let tmp = tempfile::tempdir().unwrap();
    let repo = tmp.path().join("repo");
    fs::create_dir(&repo).unwrap();
    git(&repo, &["init", "-qb", "main"]);
    fs::write(repo.join("one"), "one").unwrap();
    git(&repo, &["add", "."]);
    git(&repo, &["commit", "-qm", "initial"]);
    let tree = tmp.path().join("tree");
    git(
        &repo,
        &["worktree", "add", "-qb", "topic", tree.to_str().unwrap()],
    );
    fs::write(tree.join("two"), "two").unwrap();
    git(&tree, &["add", "."]);
    git(&tree, &["commit", "-qm", "topic"]);
    git(&repo, &["checkout", "-qb", "batch"]);
    git(&repo, &["merge", "--no-ff", "topic", "-m", "batch merge"]);
    git(&repo, &["checkout", "main"]);
    git(&repo, &["merge", "--no-ff", "batch", "-m", "outer merge"]);
    git(&repo, &["update-ref", "refs/remotes/origin/main", "HEAD"]);
    let trees = parse_porcelain(&git(&repo, &["worktree", "list", "--porcelain"])).unwrap();
    let current = &trees[1];
    let mut protection = Protection {
        guards: Guards::default(),
        claims: Claims {
            paths: vec![],
            branches: vec![],
        },
        units: vec![],
        unit_refs: vec![],
        cwds: vec![],
    };
    let now = SystemTime::now() + Duration::from_secs(90000);
    let f = facts(&repo, current, &protection, &[], now).unwrap();
    assert!(f.merged_into_main && f.pushed && eligible(current, &f, &trees));
    protection.cwds.push(tree.join("sub"));
    assert!(!eligible(
        current,
        &facts(&repo, current, &protection, &[], now).unwrap(),
        &trees
    ));
    protection.cwds.clear();
    protection.units.push(tree.clone());
    assert!(!eligible(
        current,
        &facts(&repo, current, &protection, &[], now).unwrap(),
        &trees
    ));
    protection.units.clear();
    protection.claims.branches.push("topic".into());
    assert!(!eligible(
        current,
        &facts(&repo, current, &protection, &[], now).unwrap(),
        &trees
    ));
    protection.claims.branches.clear();
    let open = Pull {
        number: 7,
        state: "OPEN".into(),
        sha: current.head.clone(),
    };
    assert!(!eligible(
        current,
        &facts(&repo, current, &protection, &[open], now).unwrap(),
        &trees
    ));
    let row = restore_row(&repo, current, Some(7));
    assert!(row.contains(&format!("\t{}\t7\n", current.head)));
    fs::write(tree.join("dirty"), "valuable").unwrap();
    assert!(remove(&repo, current).is_err());
    assert!(tree.join("dirty").exists());
    fs::remove_file(tree.join("dirty")).unwrap();
    remove(&repo, current).unwrap();
    assert!(!tree.exists());
    assert!(!git(&repo, &["branch", "--list", "topic"]).contains("topic"));
}
#[test]
fn merged_sweep_missing_reader_and_pinned_names_fail_closed() {
    let tmp = tempfile::tempdir().unwrap();
    let pins = tmp.path().join("pins.json");
    fs::write(&pins, "[]").unwrap();
    assert!(Protection::load(&pins, tmp.path(), None).is_err());
    assert!(Guards::default().protects(Path::new("/tmp/plangraph-md-sync")));
    assert!(Guards::default().protects(Path::new("/tmp/live-keeper")));
}
#[test]
fn unrecognized_run_status_refuses_all_protection_authority() {
    let tmp = tempfile::tempdir().unwrap();
    let pins = tmp.path().join("pins.json");
    fs::write(&pins, "[]").unwrap();
    let record = tmp.path().join("runs/future/record.json");
    fs::create_dir_all(record.parent().unwrap()).unwrap();
    fs::write(
        record,
        serde_json::json!({"status":"future-terminal", "checkout":tmp.path().join("elsewhere")})
            .to_string(),
    )
    .unwrap();
    assert!(Guards::load(&pins, tmp.path()).is_err());
}

#[test]
fn relocated_run_roots_still_require_readable_terminal_ownership() {
    use pg_worker_core::sweep_facts::terminal_run;
    use std::os::unix::fs::symlink;
    let tmp = tempfile::tempdir().unwrap();
    let state = tmp.path().join("state");
    let repo = tmp.path().join("repo");
    let native = tmp.path().join("relocated-runs");
    let legacy = tmp.path().join("relocated-worktrees");
    for path in [&state, &repo, &native, &legacy] {
        fs::create_dir(path).unwrap();
    }
    symlink(&native, state.join("runs")).unwrap();
    symlink(&legacy, state.join("worktrees")).unwrap();
    git(&repo, &["init", "-qb", "main"]);
    fs::write(repo.join("one"), "one").unwrap();
    git(&repo, &["add", "."]);
    git(&repo, &["commit", "-qm", "initial"]);
    for (path, record) in [
        (
            state.join("runs/native/worktree"),
            native.join("native/record.json"),
        ),
        (
            state.join("worktrees/attempt"),
            native.join("owner/record.json"),
        ),
    ] {
        git(
            &repo,
            &["worktree", "add", "--detach", path.to_str().unwrap()],
        );
        let trees = parse_porcelain(&git(&repo, &["worktree", "list", "--porcelain"])).unwrap();
        let resolved = path.canonicalize().unwrap();
        assert!(trees.iter().any(|tree| Path::new(&tree.path) == resolved));
        assert!(
            terminal_run(&state, &resolved).is_err(),
            "orphan must protect"
        );
        fs::create_dir_all(record.parent().unwrap()).unwrap();
        for status in ["running", "completed"] {
            fs::write(
                &record,
                serde_json::json!({"status":status,"checkout":resolved}).to_string(),
            )
            .unwrap();
            assert_eq!(
                terminal_run(&state, &resolved).is_ok(),
                status == "completed"
            );
        }
        fs::write(&record, "unreadable record").unwrap();
        assert!(terminal_run(&state, &resolved).is_err());
        // Leave a valid unrelated native record while testing legacy ownership.
        fs::write(
            &record,
            serde_json::json!({"status":"completed","checkout":resolved}).to_string(),
        )
        .unwrap();
    }
}

#[test]
fn merged_sweep_cli_refuses_conflicting_flags() {
    let output = Command::new(env!("CARGO_BIN_EXE_pg-worktree-sweep"))
        .args(["--apply", "--dry-run"])
        .output()
        .unwrap();
    assert!(!output.status.success());
    assert!(String::from_utf8_lossy(&output.stderr).contains("conflicting"));
}

#[test]
#[serial_test::serial]
fn sha_shaped_branches_keep_branch_pr_authority() {
    use std::os::unix::fs::PermissionsExt;
    let tmp = tempfile::tempdir().unwrap();
    let gh = tmp.path().join("gh");
    fs::write(
        &gh,
        format!("#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$0.args\"\nprintf '%s\\n' '[{{\"number\":1,\"state\":\"OPEN\",\"headRefOid\":\"{}\"}},{{\"number\":2,\"state\":\"OPEN\",\"headRefOid\":\"{}\"}}]'\n", "a".repeat(40), "b".repeat(40)),
    )
    .unwrap();
    fs::set_permissions(&gh, fs::Permissions::from_mode(0o700)).unwrap();
    let original = std::env::var_os("PATH");
    let mut paths = vec![tmp.path().to_path_buf()];
    paths.extend(std::env::split_paths(
        original.as_deref().unwrap_or_default(),
    ));
    std::env::set_var("PATH", std::env::join_paths(paths).unwrap());
    let sha = "a".repeat(40);
    let branch = pg_worker_core::sweep_facts::pulls(tmp.path(), &format!("refs/heads/{sha}"));
    let branch_args = fs::read_to_string(gh.with_extension("args"));
    let detached = pg_worker_core::sweep_facts::pulls(tmp.path(), &sha);
    let detached_args = fs::read_to_string(gh.with_extension("args"));
    match original {
        Some(value) => std::env::set_var("PATH", value),
        None => std::env::remove_var("PATH"),
    }
    branch.unwrap();
    let detached = detached.unwrap();
    assert_eq!(detached.len(), 1);
    assert_eq!(detached[0].number, 1);
    assert!(branch_args.unwrap().contains(&format!("--head\n{sha}\n")));
    let args = detached_args.unwrap();
    assert!(args.contains("--state\nopen\n"));
    assert!(!args.contains("--search") && !args.contains("--head"));
}
