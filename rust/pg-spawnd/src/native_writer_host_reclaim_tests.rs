use super::*;
use std::time::{Duration, SystemTime};

fn git_in(dir: &Path, args: &[&str]) -> String {
    let out = pg_worker_core::git_test_support::command(dir)
        .args(args)
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    String::from_utf8_lossy(&out.stdout).trim().into()
}

/// A cargo target of ten idle build variants, 1 043 bytes in all.
fn over_cap_target(dir: &Path) -> PathBuf {
    let target = dir.join("shared-target");
    let build = target.join("debug/build");
    fs::create_dir_all(&build).unwrap();
    fs::write(target.join("debug/.cargo-lock"), []).unwrap();
    fs::write(
        target.join("CACHEDIR.TAG"),
        b"Signature: 8a477f597d28d172789f06886806bc55",
    )
    .unwrap();
    let old = SystemTime::now() - Duration::from_secs(7 * 3600);
    for n in 0..10 {
        let unit = build.join(format!("pg-core-{n:016x}"));
        fs::create_dir_all(&unit).unwrap();
        fs::write(unit.join("build-script-build"), vec![0u8; 100]).unwrap();
        for p in [unit.join("build-script-build"), unit.clone()] {
            fs::File::open(p).unwrap().set_modified(old).unwrap();
        }
    }
    target
}

#[test]
fn host_reclaims_an_over_cap_target_under_the_lease_and_then_pushes_the_same_head() {
    let dir = tempfile::tempdir().unwrap();
    let remote = dir.path().join("remote.git");
    let repo = dir.path().join("repo");
    fs::create_dir_all(&remote).unwrap();
    fs::create_dir_all(&repo).unwrap();
    git_in(&remote, &["init", "-q", "--bare"]);
    git_in(&repo, &["init", "-q", "-b", "main"]);
    git_in(
        &repo,
        &["remote", "add", "origin", remote.to_str().unwrap()],
    );
    git_in(&repo, &["commit", "--allow-empty", "-qm", "accepted"]);
    let head = git_in(&repo, &["rev-parse", "HEAD"]);
    fs::create_dir_all(dir.path().join("run")).unwrap();
    let mut host = Host {
        repo: repo.clone(),
        run_id: "run-reclaim".into(),
        cwd: repo.clone(),
        reviews: dir.path().join("reviews"),
        record_path: dir.path().join("run/record.json"),
        base_ref: "refs/heads/main".into(),
        tool_path: std::env::var("PATH").unwrap(),
        effort: WRITER_EFFORT.into(),
        state: dir.path().to_path_buf(),
        brief: "brief".into(),
        push_ref: Some("refs/heads/lane/fixture".into()),
        reviews_started: 0,
        review_override: None,
        review_cli: vec!["false".into()],
        python_src: None,
    };
    let target = over_cap_target(dir.path());
    let cap = 700;
    let idle = || Ok((vec![], false));

    // A live Cargo command holds the shared lease: the reclaim refuses and names its pid.
    let command = pg_worker_core::cargo_cache::writer_lease(&target).unwrap();
    let refused = host.reclaim_at(&target, cap, idle).unwrap_err();
    assert!(
        refused.starts_with("cargo_target_not_reclaimed:"),
        "{refused}"
    );
    assert!(
        refused.contains(&format!("held by pid {}", std::process::id())),
        "{refused}"
    );
    assert!(git_in(&remote, &["branch", "--list"]).is_empty());

    // Between commands the host takes the exclusive lease, trims, and the same head is pushed.
    drop(command);
    host.reclaim_at(&target, cap, idle).unwrap();
    assert!(pg_worker_core::disk_hygiene::scan(&target).unwrap().0 <= cap);
    assert_eq!(
        host.push(&head).unwrap().as_deref(),
        Some("refs/heads/lane/fixture")
    );
    assert_eq!(
        git_in(&remote, &["rev-parse", "refs/heads/lane/fixture"]),
        head
    );
    assert!(pg_worker_core::cargo_cache::try_exclusive_lease(&target)
        .unwrap()
        .is_some());
}

#[test]
fn host_without_a_push_ref_reclaims_nothing() {
    let dir = tempfile::tempdir().unwrap();
    fs::create_dir_all(dir.path().join("run")).unwrap();
    let mut host = Host {
        repo: dir.path().into(),
        run_id: "run-none".into(),
        cwd: dir.path().into(),
        reviews: dir.path().join("reviews"),
        record_path: dir.path().join("run/record.json"),
        base_ref: "refs/heads/main".into(),
        tool_path: std::env::var("PATH").unwrap(),
        effort: WRITER_EFFORT.into(),
        state: dir.path().to_path_buf(),
        brief: "brief".into(),
        push_ref: None,
        reviews_started: 0,
        review_override: None,
        review_cli: vec!["false".into()],
        python_src: None,
    };
    assert!(host.reclaim_target().is_ok());
}
