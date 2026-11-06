use std::sync::atomic::{AtomicUsize, Ordering};
use crate::harness::{Harness, HarnessRun};

fn test_card() -> String { format!("c1-{}", std::thread::current().name().unwrap_or("attempt")) }
#[test]
fn engine_and_activity_entry_refuse_unlisted_before_setup() {
    let root = tempfile::tempdir().unwrap();
    let inp = input(Claim { card: "s2-unlisted".into(), ..Default::default() }, 1, root.path());
    let cfg = Config { card_allowlist: Some(["listed".into()].into()), ..tiny() };
    let h = Fake::new("true");
    for error in [
        run_attempt_with(Some(&h), &cfg, &Patterns::default(), &inp, &|| false).unwrap_err(),
        run_attempt(&h, &cfg, &Patterns::default(), &inp, &|| false).unwrap_err(),
    ] {
        assert!(matches!(error, AttemptError::Admission(_)));
        crate::card_admit::tests::assert_failure(crate::card_admit::attempt_error(error.into()), Some("card_not_allowlisted"), true);
    }
    let error = crate::card_admit::run_attempt(Some(&h), &cfg, &Patterns::default(), &inp, &|| false).unwrap_err();
    crate::card_admit::tests::assert_failure(crate::card_admit::attempt_error(error), Some("card_not_allowlisted"), true);
    assert_eq!(h.runs(), 0);
    assert_eq!(std::fs::read_dir(root.path()).unwrap().count(), 0);
}

#[test]
fn listed_activity_holds_owner_through_harness_and_releases_on_return() {
    struct Overlap<'a> { inp: &'a AttemptInput, cfg: &'a Config, calls: AtomicUsize }
    impl Harness for Overlap<'_> {
        fn name(&self) -> &str { "overlap" }
        fn command(&self, run: &HarnessRun) -> Command {
            self.calls.fetch_add(1, Ordering::SeqCst);
            let never = Fake::new("true");
            let error = run_attempt_with(Some(&never), self.cfg, &Patterns::default(), self.inp, &|| false).unwrap_err();
            assert!(error.to_string().contains(&self.inp.claim.card));
            crate::card_admit::tests::assert_failure(crate::card_admit::attempt_error(error.into()), Some("card_attempt_overlap"), true);
            let error = crate::card_admit::run_attempt(Some(&never), self.cfg, &Patterns::default(), self.inp, &|| false).unwrap_err();
            crate::card_admit::tests::assert_failure(crate::card_admit::attempt_error(error), Some("card_attempt_overlap"), true);
            assert_eq!(never.runs(), 0);
            let mut cmd = Command::new("sh");
            cmd.args(["-c", "echo done > out.txt"]).current_dir(run.workdir);
            cmd
        }
    }
    let (repo, base) = repo();
    let root = tempfile::tempdir().unwrap();
    let inp = input(claim(&repo.path().to_string_lossy(), &base, &["out.txt"], "true"), 1, root.path());
    let cfg = Config { card_allowlist: Some([inp.claim.card.clone()].into()), ..tiny() };
    let h = Overlap { inp: &inp, cfg: &cfg, calls: AtomicUsize::new(0) };
    for n in 1..=2 {
        let mut next = inp.clone();
        next.n = n;
        crate::card_admit::run_attempt(Some(&h), &cfg, &Patterns::default(), &next, &|| false).unwrap();
    }
    assert_eq!(h.calls.load(Ordering::SeqCst), 2);
}

    #[test]
    fn attempts_have_private_tmp_directories() {
        let first = super::attempt_tmp().unwrap();
        let second = super::attempt_tmp().unwrap();
        assert_ne!(first.path(), second.path());
        assert_ne!(first.path(), std::env::temp_dir());
        assert_ne!(second.path(), std::env::temp_dir());
        let path = first.path().to_path_buf();
        drop(first);
        assert!(!path.exists());
    }
