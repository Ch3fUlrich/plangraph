    #[test]
    fn gate_cancel_kills_the_child() {
        let d = tempfile::tempdir().unwrap();
        let pid_file = d.path().join("child.pid");
        let cfg = Config {
            start_to_close: Duration::from_secs(30),
            ..tiny()
        };
        let input = gate_input(
            d.path(),
            vec![named_check(
                "card-check",
                "sleep 60 & echo $! > child.pid; wait",
                Some(30),
            )],
        );
        let flag = Arc::new(AtomicBool::new(false));
        let start = Instant::now();

        let watching = flag.clone();
        let handle =
            std::thread::spawn(move || do_gate(&cfg, &input, &|| watching.load(Ordering::SeqCst)));

        // Wait (up to 5 s) for the command to record the grandchild pid, then
        // cancel: the group must die well before its 30 s timeout.
        let mut seen = false;
        for _ in 0..500 {
            if pid_file.exists() {
                seen = true;
                break;
            }
            std::thread::sleep(Duration::from_millis(10));
        }
        assert!(seen, "the gate never recorded its child pid");
        flag.store(true, Ordering::SeqCst);

        let outcome = handle.join().unwrap();
        assert!(start.elapsed() < Duration::from_secs(15));
        assert!(
            matches!(outcome, Ok(GateRun::Cancelled)),
            "a cancelled gate must report GateRun::Cancelled"
        );

        let pid = std::fs::read_to_string(&pid_file).unwrap();
        let pid = pid.trim();
        if Path::new("/proc/self").exists() {
            let mut dead = false;
            for _ in 0..300 {
                if !pid_alive(pid) {
                    dead = true;
                    break;
                }
                std::thread::sleep(Duration::from_millis(10));
            }
            assert!(dead, "backgrounded sleep {pid} is still alive");
        }
    }

    #[test]
    fn gate_runtime_io_error_stays_retryable() {
        let ActivityError::Application(f) = gate_error(std::io::Error::other("boom")) else {
            panic!("expected an application failure")
        };
        assert!(!f.is_non_retryable());
    }

    /// This test pins the generated activity names and the JSON shape of
    /// GateInput/GateResult. The register_activities call only proves
    /// registration does not panic: it cannot assert the registry contents,
    /// because ActivityDefinitions::names/get are pub(crate) in temporalio-sdk
    /// and there is no public accessor. Presence of `gate` in the registry is
    /// covered by the e2e worker test only if that test calls it (not yet).
    #[test]
    fn gate_activity_name_and_payload_shapes_are_stable() {
        assert_eq!(CardActivities::gate.name(), "CardActivities::gate");
        assert_eq!(CardActivities::observe.name(), "observe");
        assert_eq!(CardActivities::attempt.name(), "attempt");
        assert_eq!(CardActivities::probe.name(), "CardActivities::probe");
        assert_eq!(CardActivities::merge.name(), "CardActivities::merge");
        let mut defs = temporalio_sdk::activities::ActivityDefinitions::default();
        defs.register_activities(CardActivities::new(deps()));
        // `workroot`, `workdir` and `checks` are required on the wire; only a
        // check's `timeout_s` may be left out.
        let i: GateInput = serde_json::from_str(
            r#"{"workroot":"/w","workdir":"/w","checks":[{"name":"card-check","cmd":"true"}]}"#,
        )
        .unwrap();
        assert_eq!(i.checks.len(), 1);
        assert_eq!(i.checks[0].timeout_s, None);
        let json = serde_json::to_string(&i).unwrap();
        assert_eq!(
            json,
            r#"{"workroot":"/w","workdir":"/w","checks":[{"name":"card-check","cmd":"true"}]}"#
        );
        // A missing `checks` must fail the deserialisation, as a missing `cmd`
        // did before the list: a producer that cannot name its checks must not
        // pass as a silent no-op gate.
        assert!(serde_json::from_str::<GateInput>(r#"{"workroot":"/w","workdir":"/w"}"#).is_err());
        let r = GateResult {
            passed: true,
            checks: vec![GateCheckResult {
                name: "card-check".to_string(),
                passed: true,
                exit_code: Some(0),
                signal: None,
                timed_out: false,
                tail: "t".into(),
                output_hash: "h".into(),
            }],
        };
        assert_eq!(
            serde_json::to_string(&r).unwrap(),
            r#"{"passed":true,"checks":[{"name":"card-check","passed":true,"exit_code":0,"signal":null,"timed_out":false,"tail":"t","output_hash":"h"}]}"#
        );
        let back: GateResult = serde_json::from_str(&serde_json::to_string(&r).unwrap()).unwrap();
        assert_eq!(back, r);
    }

    #[test]
    fn phase_branch_divergence_is_non_retryable_with_or_without_creation_race() {
        for race in [false, true] {
            let (dir, base) = repo();
            let input = PhaseBranchInput {
                repo: dir.path().to_str().unwrap().into(),
                base_branch: "main".into(),
                branch: "phase/race".into(),
            };
            if !race {
                git(dir.path(), &["branch", "phase/race"]);
            }
            let mut injected = false;
            let result = ensure_phase_branch_with(&input, |path, args| {
                let result = pg_worker_core::git::git(path, args);
                if !injected && args.contains(&"--verify") {
                    injected = true;
                    git(path, &["commit", "--allow-empty", "-m", "advance"]);
                    if race {
                        git(path, &["branch", "phase/race", &base]);
                    }
                }
                result
            });
            assert!(injected);
            let error = result.unwrap_err();
            assert_eq!(
                error.to_string(),
                "phase branch does not contain the base head",
                "race={race}"
            );
            let ActivityError::Application(failure) = phase_branch_error(error) else {
                panic!("expected application failure");
            };
            assert!(failure.is_non_retryable(), "race={race}");
        }
    }

    #[test]
    fn phase_branch_exit_one_retries_absent_ref_but_adopts_base_head() {
        for appears in [false, true] {
            let (dir, base) = repo();
            let input = PhaseBranchInput {
                repo: dir.path().to_str().unwrap().into(),
                base_branch: "main".into(),
                branch: "phase/race".into(),
            };
            let result = ensure_phase_branch_with(&input, |path, args| {
                if args[0] == "update-ref" {
                    if appears {
                        git(path, &["branch", "phase/race", &base]);
                    }
                    return Err(pg_worker_core::git::GitError::Failed {
                        args: String::new(),
                        code: Some(1),
                        stderr: String::new(),
                    });
                }
                pg_worker_core::git::git(path, args)
            });
            if appears {
                assert_eq!(result.map(|r| (r.created, r.head)).unwrap(), (false, base));
            } else {
                assert!(matches!(phase_branch_error(result.unwrap_err()),
                    ActivityError::Application(failure) if !failure.is_non_retryable()));
            }
        }
    }

    #[test]
    fn ensure_phase_branch_creates_and_is_idempotent() {
        let (dir, base) = repo();
        let path = dir.path().to_str().unwrap();
        let input = PhaseBranchInput {
            repo: path.to_string(),
            base_branch: "main".to_string(),
            branch: "phase/p1".to_string(),
        };
        let first = do_ensure_phase_branch(&input).unwrap();
        assert!(first.created);
        assert_eq!(first.head, base);
        assert_eq!(git(dir.path(), &["rev-parse", "refs/heads/phase/p1"]), base);
        let second = do_ensure_phase_branch(&input).unwrap();
        assert!(!second.created);
        assert_eq!(second.head, base);
        git(dir.path(), &["checkout", "phase/p1"]);
        git(dir.path(), &["commit", "--allow-empty", "-m", "phase"]);
        assert!(!do_ensure_phase_branch(&input).unwrap().created);
        git(dir.path(), &["checkout", "main"]);
        git(dir.path(), &["commit", "--allow-empty", "-m", "base"]);
        assert_eq!(
            do_ensure_phase_branch(&input).unwrap_err().to_string(),
            "phase branch does not contain the base head"
        );
    }
    #[test]
    fn ensure_phase_branch_refuses_bad_input() {
        let (dir, _) = repo();
        let path = dir.path().to_str().unwrap();
        let same = PhaseBranchInput {
            repo: path.to_string(),
            base_branch: "phase/x".to_string(),
            branch: "phase/x".to_string(),
        };
        assert_eq!(
            do_ensure_phase_branch(&same).unwrap_err().to_string(),
            "phase branch must not be the base branch"
        );
        let not_phase = PhaseBranchInput {
            repo: path.to_string(),
            base_branch: "main".to_string(),
            branch: "main".to_string(),
        };
        assert_eq!(
            do_ensure_phase_branch(&not_phase).unwrap_err().to_string(),
            "not a phase/ branch"
        );
        let evil = PhaseBranchInput {
            repo: path.to_string(),
            base_branch: "-evil".to_string(),
            branch: "phase/p1".to_string(),
        };
        assert!(do_ensure_phase_branch(&evil).is_err());
        let missing = PhaseBranchInput {
            repo: path.to_string(),
            base_branch: "nope".to_string(),
            branch: "phase/p1".to_string(),
        };
        assert!(do_ensure_phase_branch(&missing).is_err());
    }
    #[test]
    fn phase_plan_pure_body_uses_keeper() {
        let keeper = StubKeeper::default();
        let plan: crate::phase::PhasePlan =
            serde_json::from_str(r#"{"phase":"p1","ready":true,"blocked":[],"cards":[]}"#).unwrap();
        keeper.push_plan(plan.clone());

        assert_eq!(do_phase_plan(&keeper, "p1").unwrap(), plan);
        assert_eq!(keeper.plan_calls(), vec!["p1".to_string()]);
    }

    #[test]
    fn finish_is_refusal_maps_400_and_refusal_statuses() {
        for status in [400, 401, 403, 409, 422] {
            assert!(finish_is_refusal(&crate::keeper::KeeperError::Status(
                status
            )));
        }
        for status in [404, 500, 503] {
            assert!(!finish_is_refusal(&crate::keeper::KeeperError::Status(
                status
            )));
        }
        assert!(!finish_is_refusal(&crate::keeper::KeeperError::Decode(
            "x".into()
        )));
    }

    #[test]
    fn phase_finish_pure_body_uses_keeper() {
        let keeper = StubKeeper::default();
        let finish = crate::phase::PhaseFinish {
            state: crate::phase::FINISH_MERGED.to_string(),
            pr: Some(crate::phase::PhasePr {
                number: 7,
                url: "u".to_string(),
            }),
            detail: String::new(),
        };
        keeper.push_finish(finish.clone());

        let got = do_phase_finish(&keeper, "p1", "phase/p1", "head1", None).unwrap();
        assert_eq!(got.state, finish.state);
        assert_eq!(
            got.pr.as_ref().map(|p| (p.number, p.url.as_str())),
            Some((7, "u"))
        );
        assert_eq!(got.detail, finish.detail);
        assert_eq!(
            keeper.finish_calls(),
            vec![(
                "p1".to_string(),
                "phase/p1".to_string(),
                "head1".to_string()
            )]
        );
    }

    include!("activities_claim_tests.rs");
