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
