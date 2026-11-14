    #[test]
    fn keeper_lease_releases_on_panic() {
        let keeper = StubKeeper::default();
        let fence = keeper.acquire_merge_lock("/r", "o", 60).unwrap();
        assert!(keeper.merge_lock_held());
        let r = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            let _lease = KeeperLease {
                keeper: &keeper,
                repo: "/r",
                fence,
            };
            panic!("merge blew up");
        }));
        assert!(r.is_err());
        assert!(!keeper.merge_lock_held());
    }

    #[test]
    fn merge_lock_ttl_covers_the_merge_activity_window() {
        let mut cfg = tiny();
        cfg.start_to_close = Duration::from_secs(900);
        assert!(merge_lock_ttl_s(&cfg) >= 900 + MERGE_LOCK_TIMEOUT.as_secs());
    }

    #[test]
    fn do_merge_rechecks_confinement_under_the_lock() {
        let (r, base) = repo();
        let p = r.path();
        attempt_branch(p, "attempt/c1/1", "secret.txt", "x", &base);
        let mut input = merge_input(p, "attempt/c1/1", &base);
        input.confine = Some(ConfineSpec {
            allow: vec!["src/**".to_string()],
            forbid: vec![],
        });
        let keeper = StubKeeper::default();
        let res = merge(&keeper, &input);
        assert_eq!(res.outcome, "contract_defect");
        assert!(res.detail.contains("secret.txt"), "{}", res.detail);
        assert_eq!(git(p, &["rev-parse", "main"]), base);
        assert!(!keeper.merge_lock_held());

        // Inside the write set it merges.
        input.confine = Some(ConfineSpec {
            allow: vec!["*.txt".to_string()],
            forbid: vec![],
        });
        assert_eq!(merge(&keeper, &input).outcome, "merged");
    }

    #[test]
    fn do_merge_rebases_when_main_moved_and_checks_once() {
        let (r, base) = repo();
        let p = r.path();
        let root = tempfile::tempdir().unwrap();
        attempt_worktree(p, root.path(), "c1", "x.txt", "x", &base);
        write(p, "y.txt", "y");
        let moved = commit_all(p, "advance");

        let mut input = merge_input(p, &AttemptId::new("c1", 1).branch(), &base);
        input.worktree = root
            .path()
            .join(AttemptId::new("c1", 1).dir_name())
            .to_string_lossy()
            .into_owned();
        // The check sees the rebased tree: both files must be present.
        input.check_cmd = Some("test -f x.txt && test -f y.txt".to_string());
        let res = merge(&StubKeeper::default(), &input);
        assert_eq!(res.outcome, "merged", "{res:?}");
        let sha = res.sha.unwrap();
        assert_eq!(git(p, &["rev-parse", "main"]), sha);
        assert_eq!(git(p, &["rev-parse", "main~1"]), moved);
    }

    #[test]
    fn do_merge_rebase_conflict_is_a_conflict() {
        let (r, base) = repo();
        let p = r.path();
        let root = tempfile::tempdir().unwrap();
        attempt_worktree(p, root.path(), "c1", "README.md", "attempt\n", &base);
        write(p, "README.md", "main\n");
        let moved = commit_all(p, "main edit");

        let mut input = merge_input(p, &AttemptId::new("c1", 1).branch(), &base);
        input.worktree = root
            .path()
            .join(AttemptId::new("c1", 1).dir_name())
            .to_string_lossy()
            .into_owned();
        let res = merge(&StubKeeper::default(), &input);
        assert_eq!(res.outcome, "conflict");
        assert_eq!(git(p, &["rev-parse", "main"]), moved);
    }

    #[test]
    fn do_merge_failing_post_rebase_check_is_check_fail() {
        let (r, base) = repo();
        let p = r.path();
        let root = tempfile::tempdir().unwrap();
        attempt_worktree(p, root.path(), "c1", "x.txt", "x", &base);
        write(p, "y.txt", "y");
        let moved = commit_all(p, "advance");

        let mut input = merge_input(p, &AttemptId::new("c1", 1).branch(), &base);
        input.worktree = root
            .path()
            .join(AttemptId::new("c1", 1).dir_name())
            .to_string_lossy()
            .into_owned();
        input.check_cmd = Some("echo broke after rebase; exit 1".to_string());
        let keeper = StubKeeper::default();
        let res = merge(&keeper, &input);
        assert_eq!(res.outcome, "check_fail");
        assert!(res.detail.contains("broke after rebase"));
        assert_eq!(git(p, &["rev-parse", "main"]), moved);
        assert!(!keeper.merge_lock_held());
    }

    #[test]
    fn merge_budget_sits_inside_the_window_and_the_lock_ttl_covers_it() {
        for secs in [4u64, 900, 5400] {
            let mut cfg = tiny();
            cfg.start_to_close = Duration::from_secs(secs);
            assert!(merge_work_budget(&cfg) < merge_window(&cfg), "{secs}");
            assert!(merge_work_budget(&cfg) + merge_reserve(&cfg) <= merge_window(&cfg));
            assert!(merge_reserve(&cfg) <= Duration::from_secs(60));
            assert!(
                merge_lock_ttl_s(&cfg)
                    >= merge_window(&cfg).as_secs() + MERGE_LOCK_TIMEOUT.as_secs()
            );
        }
    }

    #[test]
    fn do_merge_post_rebase_check_honours_the_merge_deadline() {
        let (r, base) = repo();
        let p = r.path();
        let root = tempfile::tempdir().unwrap();
        attempt_worktree(p, root.path(), "c1", "x.txt", "x", &base);
        write(p, "y.txt", "y");
        let moved = commit_all(p, "advance");

        let mut input = merge_input(p, &AttemptId::new("c1", 1).branch(), &base);
        input.worktree = root
            .path()
            .join(AttemptId::new("c1", 1).dir_name())
            .to_string_lossy()
            .into_owned();
        // A check far longer than the whole merge window.
        input.check_cmd = Some("sleep 120".to_string());
        let mut cfg = tiny();
        cfg.start_to_close = Duration::from_secs(4);
        let keeper = StubKeeper::default();
        let t = Instant::now();
        let res = do_merge(&keeper, &cfg, &input).unwrap();
        assert!(t.elapsed() < Duration::from_secs(20), "{:?}", t.elapsed());
        assert_eq!(res.outcome, "check_fail", "{res:?}");
        assert!(res.detail.contains("merge budget"), "{}", res.detail);
        // Nothing landed, and the keeper lock came back.
        assert_eq!(git(p, &["rev-parse", "main"]), moved);
        assert!(!keeper.merge_lock_held());
    }

    #[test]
    fn do_janitor_removes_dead_attempts() {
        let (r, base) = repo();
        let root = tempfile::tempdir().unwrap();
        let id = AttemptId::new("c1", 1);
        let wt = worktree::create(
            r.path(),
            &base,
            &root.path().join(AttemptId::new("c1", 1).dir_name()),
            &id,
        )
        .unwrap();
        write(&wt.path, "half.txt", "x");
        commit_all(&wt.path, "work");

        let input = JanitorInput {
            repo: r.path().to_string_lossy().into_owned(),
            workroot: root.path().to_string_lossy().into_owned(),
            check_cmd: "test -f other.txt".to_string(),
            base_sha: base,
            card: "c1".to_string(),
        };
        // Commit beyond base but a failing check is `partial`, so it is removed.
        assert_eq!(do_janitor(&tiny(), &input).unwrap(), 1);
        assert!(!wt.path.exists());
    }

    #[test]
    fn gate_builtin_validation_and_legacy_json() {
        let json =
            r#"{"workroot":".","workdir":".","checks":[{"name":"scan","cmd":"placeholder"}]}"#;
        let mut input: GateInput = serde_json::from_str(json).unwrap();
        assert_eq!(serde_json::to_string(&input).unwrap(), json);
        input.checks[0].builtin = Some("unknown".into());
        assert_eq!(
            do_gate(&tiny(), &input, &|| false).unwrap_err().kind(),
            std::io::ErrorKind::InvalidInput
        );
        input.checks[0].builtin = Some("diffscan".into());
        assert_eq!(
            do_gate(&tiny(), &input, &|| false).unwrap_err().kind(),
            std::io::ErrorKind::InvalidInput
        );
    }

    #[test]
    fn gate_diffscan_moved_test_is_a_deletion() {
        let (r, _) = repo();
        std::fs::create_dir(r.path().join("tests")).unwrap();
        std::fs::create_dir(r.path().join("docs")).unwrap();
        write(
            r.path(),
            "tests/test_a.py",
            "def test_a():\n    assert True\n",
        );
        let base = commit_all(r.path(), "test");
        git(r.path(), &["mv", "tests/test_a.py", "docs/check_a.py"]);
        commit_all(r.path(), "move test out of tests");
        let mut scan = named_check("scan", "exit 99", None);
        scan.builtin = Some("diffscan".into());
        let mut input = gate_input(r.path(), vec![scan]);
        input.base_sha = Some(base);
        let result = run(&tiny(), &input);
        assert!(!result.passed);
        assert!(
            result.checks[0]
                .tail
                .contains("deleted test file in tests/test_a.py")
        );
    }

    #[test]
    fn gate_diffscan_clean_deleted_test_and_later_check() {
        let (r, _) = repo();
        write(r.path(), "test_x.py", "def test_x():\n    assert True\n");
        let base = commit_all(r.path(), "test");
        let mut scan = named_check("scan", "exit 99", None);
        scan.builtin = Some("diffscan".into());
        let mut input = gate_input(r.path(), vec![scan, named_check("later", "true", None)]);
        input.base_sha = Some(base);
        assert!(run(&tiny(), &input).passed);
        std::fs::remove_file(r.path().join("test_x.py")).unwrap();
        commit_all(r.path(), "delete test");
        let result = run(&tiny(), &input);
        assert!(!result.passed);
        assert!(result.checks[0].tail.contains("deleted test"));
        assert_eq!(result.checks[0].name, "scan");
        assert!(result.checks[1].passed);
    }

    #[test]
    fn gate_exports_confines_files() {
        let (r, _) = repo();
        write(r.path(), "lib.rs", "pub fn run() {}");
        let mut check = named_check("exports", "exit 99", None);
        check.builtin = Some("exports".into());
        let mut input = gate_input(r.path(), vec![check]);
        input.exports = vec!["lib.rs:run".into()];
        assert!(run(&tiny(), &input).passed);
        input.exports.push("../lib.rs:run".into());
        let result = run(&tiny(), &input);
        assert!(!result.passed);
        assert!(result.checks[0].tail.contains("../lib.rs:run"));
    }

    #[test]
    fn do_janitor_scoped_to_one_card() {
        let (r, base) = repo();
        let root = tempfile::tempdir().unwrap();
        let c1 = AttemptId::new("c1", 1);
        let c2 = AttemptId::new("c2", 1);
        let wt1 = worktree::create(r.path(), &base, &root.path().join(c1.dir_name()), &c1).unwrap();
        let wt2 = worktree::create(r.path(), &base, &root.path().join(c2.dir_name()), &c2).unwrap();
        write(&wt1.path, "half.txt", "x");
        commit_all(&wt1.path, "work");

        let input = JanitorInput {
            repo: r.path().to_string_lossy().into_owned(),
            workroot: root.path().to_string_lossy().into_owned(),
            check_cmd: "test -f other.txt".to_string(),
            base_sha: base,
            card: "c1".to_string(),
        };

        // c1 is partial and removed; c2 belongs to another card and stays.
        assert_eq!(do_janitor(&tiny(), &input).unwrap(), 1);
        assert!(!wt1.path.exists());
        assert!(wt2.path.exists());
    }

    /// One named check for the gate tests.
    fn named_check(name: &str, cmd: &str, timeout_s: Option<u64>) -> GateCheck {
        GateCheck {
            builtin: None,
            name: name.to_string(),
            cmd: cmd.to_string(),
            timeout_s,
        }
    }

    /// A gate input that runs `checks` in `dir`, with `dir` as its own work root.
    fn gate_input(dir: &std::path::Path, checks: Vec<GateCheck>) -> GateInput {
        let workroot = dir.to_string_lossy().into_owned();
        GateInput {
            base_sha: None,
            exports: Vec::new(),
            workroot: workroot.clone(),
            workdir: workroot,
            checks,
        }
    }

    /// Run a gate that nobody cancels and take its result.
    fn run(cfg: &Config, input: &GateInput) -> GateResult {
        match do_gate(cfg, input, &|| false).unwrap() {
            GateRun::Done(result) => result,
            GateRun::Cancelled => panic!("a gate without a cancel must not report Cancelled"),
        }
    }

    #[test]
    fn gate_pass() {
        let d = tempfile::tempdir().unwrap();
        let r = run(
            &tiny(),
            &gate_input(d.path(), vec![named_check("card-check", "echo fine", None)]),
        );
        assert!(r.passed);
        assert_eq!(r.checks.len(), 1);
        let c = &r.checks[0];
        assert_eq!(c.name, "card-check");
        assert!(c.passed && !c.timed_out);
        assert_eq!(c.exit_code, Some(0));
        assert_eq!(c.tail, "fine");
        assert!(!c.output_hash.is_empty());
    }
