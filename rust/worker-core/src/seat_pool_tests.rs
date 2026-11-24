use super::*;
use crate::writer_cleanup::{save_lifecycle, tests::Fixture};

fn grant(f: &Fixture, run: &str) {
    slots::with_table(&f.state, Duration::from_secs(5), |t| {
        t.active.push(crate::slots::ActiveSlot {
            run_id: run.into(),
            pid: 1,
            start_time: 1,
            role: crate::hosts::Role::Review,
            class: SlotClass::Seat,
            repo_id: "fixture".into(),
            seat_slot: Some(1),
            group: Some("fixture".into()),
            reservation_mb: 0,
            created_ms: 0,
            bound: true,
        });
    })
    .unwrap();
}
fn workspace(f: &Fixture) -> Workspace {
    let sha =
        String::from_utf8(git(Path::new(&f.repo.root), &["rev-parse", "HEAD"]).unwrap()).unwrap();
    Workspace {
        repo_id: "fixture".into(),
        mode: Mode::Seat,
        base_sha: Some(sha.trim().into()),
        revision_sha: Some(sha.trim().into()),
        branch: None,
    }
}
pub(crate) fn seat(f: &mut Fixture) -> SeatProof {
    grant(f, "run");
    let proof = prepare_seat(&f.repo, &workspace(f), &f.state, "run", 1).unwrap();
    f.record.checkout = proof.checkout.clone();
    save_lifecycle(&f.state, &f.record).unwrap();
    proof
}
#[test]
fn granted_slot_rejects_option_ref_path() {
    let f = Fixture::new();
    grant(&f, "run");
    for sha in [
        "--recurse-submodules=on",
        "HEAD",
        "../HEAD",
        &"A".repeat(40),
    ] {
        let mut ws = workspace(&f);
        ws.revision_sha = Some(sha.into());
        assert!(matches!(
            prepare_seat(&f.repo, &ws, &f.state, "run", 1),
            Err(WriterError::Invalid)
        ));
    }
    assert!(!Path::new(&f.repo.worktrees_root).join("seat-01").exists());
}
#[test]
fn concurrent_grants_cannot_share_seat() {
    let f = Fixture::new();
    std::thread::scope(|s| {
        let a = s.spawn(|| grant(&f, "run"));
        let b = s.spawn(|| grant(&f, "other"));
        a.join().unwrap();
        b.join().unwrap();
    });
    for run in ["run", "other"] {
        assert!(prepare_seat(&f.repo, &workspace(&f), &f.state, run, 1).is_err());
    }
}
#[test]
fn detached_reset_cleans_ignored_and_untracked() {
    let mut f = Fixture::new();
    let p = seat(&mut f);
    fs::write(p.checkout.join(".gitignore"), "ignored\n").unwrap();
    fs::write(p.checkout.join("ignored"), "secret").unwrap();
    fs::write(p.checkout.join("untracked"), "scratch").unwrap();
    let p = prepare_seat(&f.repo, &workspace(&f), &f.state, "run", 1).unwrap();
    for name in [".gitignore", "ignored", "untracked"] {
        assert!(!p.checkout.join(name).exists());
    }
    assert!(git(&p.checkout, &["symbolic-ref", "HEAD"]).is_err());
}
#[test]
fn head_porcelain_before_after() {
    let mut f = Fixture::new();
    let p = seat(&mut f);
    assert_eq!(
        git(&p.checkout, &["rev-parse", "HEAD"]).unwrap(),
        format!("{}\n", p.head_at_start).as_bytes()
    );
    assert!(git(&p.checkout, &["status", "--porcelain=v1", "-z"])
        .unwrap()
        .is_empty());
    let record = finish_seat(&f.repo, &p).unwrap();
    assert_eq!(record.head_at_start, record.head_at_end);
    git(
        &p.checkout,
        &["symbolic-ref", "HEAD", "refs/heads/attached"],
    )
    .unwrap();
    assert!(finish_seat(&f.repo, &p).is_err());
    assert!(finish_seat(&f.repo, &p).is_ok());
}
fn commit_empty(f: &Fixture, message: &str) -> String {
    // Plumbing under the fixture's git isolation: no hooks, signing or host identity.
    let root = Path::new(&f.repo.root);
    let run = |args: &[&str]| {
        let out = crate::git_test_support::command(root)
            .args(args)
            .output()
            .unwrap();
        assert!(out.status.success(), "{args:?}");
        String::from_utf8(out.stdout).unwrap().trim().to_owned()
    };
    let tree = run(&["write-tree"]);
    let parent = run(&["rev-parse", "HEAD"]);
    let sha = run(&["commit-tree", &tree, "-p", &parent, "-m", message]);
    run(&["update-ref", "HEAD", &sha]);
    sha
}

#[test]
fn seat_is_pinned_to_the_requested_head_not_the_repository_head() {
    let mut f = Fixture::new();
    let first = workspace(&f).revision_sha.unwrap();
    commit_empty(&f, "second");
    grant(&f, "run");
    let mut ws = workspace(&f);
    ws.revision_sha = Some(first.clone());
    let proof = prepare_seat(&f.repo, &ws, &f.state, "run", 1).unwrap();
    assert_eq!(proof.head_at_start, first);
    let head = git(&proof.checkout, &["rev-parse", "HEAD"]).unwrap();
    assert_eq!(head, format!("{first}\n").as_bytes());
    f.record.checkout = proof.checkout.clone();
    save_lifecycle(&f.state, &f.record).unwrap();
    let record = finish_seat(&f.repo, &proof).unwrap();
    assert_eq!(
        (record.head_at_start, record.head_at_end),
        (first.clone(), first)
    );
}

#[test]
fn same_slot_is_clean_and_repinned_on_reuse() {
    let mut f = Fixture::new();
    let first = seat(&mut f);
    fs::write(first.checkout.join("leftover"), "scratch").unwrap();
    let second_head = commit_empty(&f, "second");
    let mut ws = workspace(&f);
    ws.revision_sha = Some(second_head.clone());
    let reused = prepare_seat(&f.repo, &ws, &f.state, "run", 1).unwrap();
    assert_eq!(reused.checkout, first.checkout);
    assert!(!reused.checkout.join("leftover").exists());
    assert_eq!(reused.head_at_start, second_head);
    assert!(git(&reused.checkout, &["status", "--porcelain=v1", "-z"])
        .unwrap()
        .is_empty());
    assert!(git(&reused.checkout, &["symbolic-ref", "-q", "HEAD"]).is_err());
}

#[test]
fn dirty_slot_fails_and_resets() {
    let mut f = Fixture::new();
    let p = seat(&mut f);
    fs::write(p.checkout.join("dirty"), "scratch").unwrap();
    assert!(finish_seat(&f.repo, &p).is_err());
    assert!(!p.checkout.join("dirty").exists());
    assert!(finish_seat(&f.repo, &p).is_ok());
}
#[test]
fn caches_outside_survive_reset() {
    let mut f = Fixture::new();
    let cache = f.state.join("cache");
    fs::write(&cache, "keep").unwrap();
    let p = seat(&mut f);
    fs::write(p.checkout.join("dirty"), "scratch").unwrap();
    assert!(finish_seat(&f.repo, &p).is_err());
    assert_eq!(fs::read(cache).unwrap(), b"keep");
}
#[test]
fn rejects_injection_and_missing_grants() {
    let tmp = tempfile::tempdir().unwrap();
    let repo = RepoEntry {
        live_dir: None,
        health_checks: None,
        root: tmp.path().to_string_lossy().into(),
        worktrees_root: tmp.path().to_string_lossy().into(),
        remote: "unused".into(),
        python: None,
        import_roots: vec![],
        cache_env: Default::default(),
    };
    let mut ws = Workspace {
        repo_id: "fixture".into(),
        mode: Mode::Read,
        base_sha: None,
        revision_sha: Some("a".repeat(40)),
        branch: None,
    };
    assert!(prepare_seat(&repo, &ws, tmp.path(), "run", 1).is_err());
    assert!(prepare_seat(&repo, &ws, tmp.path(), "../run", 1).is_err());
    ws.revision_sha = Some("--all".into());
    assert!(prepare_seat(&repo, &ws, tmp.path(), "run", 1).is_err());
}

#[test]
fn cache_reset_fixture_does_not_invoke_commit_signing() {
    let f = Fixture::with_config(&[
        ("commit.gpgsign", "true"),
        ("gpg.program", "plangraph-no-such-signing-program"),
    ]);
    assert!(!git(Path::new(&f.repo.root), &["rev-parse", "HEAD"])
        .unwrap()
        .is_empty());
}

#[test]
fn caches_outside_survive_reset_stress() {
    for _ in 0..50 {
        caches_outside_survive_reset();
    }
}

#[test]
fn seat_creation_waits_for_the_reaper_lock() {
    let f = Fixture::new();
    let root = Path::new(&f.repo.root);
    let dir = tempfile::tempdir().unwrap();
    let tree = dir.path().join("seat");
    let sha = workspace(&f).revision_sha.unwrap();
    let held = crate::worktree::mutation_lock(root).unwrap();
    std::thread::scope(|scope| {
        let (start, started) = std::sync::mpsc::channel();
        let (finish, finished) = std::sync::mpsc::channel();
        let (repo, dest) = (&f.repo, &tree);
        let worker = scope.spawn(move || {
            start.send(()).unwrap();
            finish.send(create(repo, dest, &sha)).unwrap();
        });
        started.recv().unwrap();
        let before_release = finished.recv_timeout(Duration::from_secs(1));
        let registered = tree.exists();
        drop(held);
        worker.join().unwrap();
        assert!(matches!(
            before_release,
            Err(std::sync::mpsc::RecvTimeoutError::Timeout)
        ));
        assert!(!registered);
        finished.recv().unwrap().unwrap();
        assert!(tree.exists());
    });
}
