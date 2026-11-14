    #[test]
    fn gate_fail_keeps_tail_and_a_stable_hash() {
        let d = tempfile::tempdir().unwrap();
        let a = run(
            &tiny(),
            &gate_input(
                d.path(),
                vec![named_check("card-check", "echo nope; exit 3", None)],
            ),
        );
        let b = run(
            &tiny(),
            &gate_input(
                d.path(),
                vec![named_check("card-check", "echo nope; exit 3", None)],
            ),
        );
        assert!(!a.passed);
        let (ca, cb) = (&a.checks[0], &b.checks[0]);
        assert!(!ca.passed && !ca.timed_out);
        assert_eq!(ca.exit_code, Some(3));
        assert!(ca.tail.contains("nope"));
        assert_eq!(ca.output_hash, cb.output_hash);
    }

    #[test]
    fn two_checks_keep_separate_tails_and_hashes() {
        let d = tempfile::tempdir().unwrap();
        let r = run(
            &tiny(),
            &gate_input(
                d.path(),
                vec![
                    named_check("first", "echo one-a; echo one-b; exit 1", None),
                    named_check("second", "echo two-c", None),
                ],
            ),
        );
        // The gate fails because ONE check failed...
        assert!(!r.passed);
        assert_eq!(r.checks.len(), 2);
        let (a, b) = (&r.checks[0], &r.checks[1]);
        assert_eq!((a.name.as_str(), b.name.as_str()), ("first", "second"));
        assert!(!a.passed && b.passed);
        // ...but each check reports its own tail, with no bleed across them.
        assert!(
            a.tail.contains("one-a") && a.tail.contains("one-b"),
            "{}",
            a.tail
        );
        assert!(!a.tail.contains("two-c"), "{}", a.tail);
        assert!(b.tail.contains("two-c"), "{}", b.tail);
        assert!(!b.tail.contains("one-a"), "{}", b.tail);
        assert_ne!(a.output_hash, b.output_hash);
        assert_eq!(a.output_hash, hash::hash_bytes(a.tail.as_bytes()));
        assert_eq!(b.output_hash, hash::hash_bytes(b.tail.as_bytes()));
    }

    /// A failing first check must not stop the later ones: a later diff scan
    /// still reports, so the card sees everything the gate saw.
    #[test]
    fn a_failing_first_check_does_not_stop_the_later_ones() {
        let d = tempfile::tempdir().unwrap();
        let r = run(
            &tiny(),
            &gate_input(
                d.path(),
                vec![
                    named_check("fails", "exit 1", None),
                    named_check("still-runs", "echo done > ran-marker", None),
                ],
            ),
        );
        assert!(!r.passed);
        assert_eq!(r.checks.len(), 2);
        assert!(!r.checks[0].passed);
        assert!(r.checks[1].passed, "{}", r.checks[1].tail);
        assert!(
            d.path().join("ran-marker").exists(),
            "the later check must still have run"
        );
    }

    #[test]
    fn gate_budget_exhaustion_marks_later_checks_not_run() {
        let d = tempfile::tempdir().unwrap();
        // The first check eats the whole budget (it is killed at the budget's
        // end); the second must be reported as not run, never half-run.
        let cfg = Config {
            start_to_close: Duration::from_millis(500),
            ..tiny()
        };
        let input = gate_input(
            d.path(),
            vec![
                named_check("slow", "sleep 30", None),
                named_check("never", "echo too late", None),
            ],
        );
        let t = Instant::now();
        let r = run(&cfg, &input);
        assert!(t.elapsed() < Duration::from_secs(20), "{:?}", t.elapsed());
        assert!(!r.passed);
        assert_eq!(r.checks.len(), 2);
        assert!(
            r.checks[0].timed_out && !r.checks[0].passed,
            "{:?}",
            r.checks[0]
        );
        let second = &r.checks[1];
        assert!(second.timed_out && !second.passed, "{second:?}");
        assert_eq!(second.exit_code, None);
        assert_eq!(second.tail, "not run: gate budget exhausted");
        assert_eq!(second.output_hash, hash::hash_bytes(second.tail.as_bytes()));
    }

    #[test]
    fn gate_timeout_is_reported_and_capped_by_start_to_close() {
        let d = tempfile::tempdir().unwrap();
        let t = Instant::now();
        let r = run(
            &tiny(),
            &gate_input(
                d.path(),
                vec![named_check("card-check", "echo start; sleep 30", Some(1))],
            ),
        );
        assert!(!r.passed);
        assert!(r.checks[0].timed_out);
        assert!(r.checks[0].tail.contains("start"));
        assert!(t.elapsed() < Duration::from_secs(20));
        // A requested timeout above start_to_close is capped to it.
        let cfg = Config {
            start_to_close: Duration::from_secs(1),
            ..tiny()
        };
        let r = run(
            &cfg,
            &gate_input(
                d.path(),
                vec![named_check("card-check", "sleep 30", Some(3600))],
            ),
        );
        assert!(r.checks[0].timed_out && !r.passed);
    }

    /// Every invalid list is rejected before ANY check runs (so the marker
    /// file a check would create never appears) and is non-retryable input,
    /// never a pass and never a retry.
    #[test]
    fn gate_invalid_lists_run_nothing_and_are_not_retryable() {
        let d = tempfile::tempdir().unwrap();
        let marker_cmd = "echo ran > ran-marker";
        let cases: Vec<(&str, Vec<GateCheck>)> = vec![
            ("empty list", vec![]),
            (
                "duplicate name",
                vec![
                    named_check("dup", "true", None),
                    named_check("dup", marker_cmd, None),
                ],
            ),
            ("blank name", vec![named_check("", marker_cmd, None)]),
            (
                "name with a space",
                vec![named_check("bad name", marker_cmd, None)],
            ),
            (
                "name with a slash",
                vec![named_check("bad/name", marker_cmd, None)],
            ),
            (
                "name longer than 64",
                vec![named_check(&"x".repeat(65), marker_cmd, None)],
            ),
            ("blank command", vec![named_check("ok-name", "   ", None)]),
            (
                "zero timeout",
                vec![named_check("ok-name", marker_cmd, Some(0))],
            ),
            // A valid check before the invalid one must not run either: the
            // whole list is validated before the first command starts.
            (
                "valid first, invalid second",
                vec![
                    named_check("runs-first", marker_cmd, None),
                    named_check("bad name", "true", None),
                ],
            ),
        ];
        for (why, checks) in cases {
            let e = do_gate(&tiny(), &gate_input(d.path(), checks), &|| false).unwrap_err();
            assert_eq!(e.kind(), std::io::ErrorKind::InvalidInput, "{why}");
            assert!(!d.path().join("ran-marker").exists(), "{why} ran a check");
        }
        let ActivityError::Application(f) = gate_error(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "gate has no checks",
        )) else {
            panic!("expected an application failure")
        };
        assert!(f.is_non_retryable());
    }

    /// The workdir is confined to the workroot: anything that leaves it — a
    /// `..` component, an absolute path, a symlink out — is refused as invalid
    /// input before the check runner is entered.
    #[test]
    fn gate_workdir_escape_is_refused() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("work");
        std::fs::create_dir(&root).unwrap();
        let outside = tmp.path().join("outside");
        std::fs::create_dir(&outside).unwrap();
        std::fs::create_dir(root.join("sub")).unwrap();
        std::os::unix::fs::symlink(&outside, root.join("link")).unwrap();

        let workroot = root.to_string_lossy().into_owned();
        let outside = outside.to_string_lossy().into_owned();
        for candidate in [
            "../outside",
            "sub/../../outside",
            outside.as_str(),
            "link",
            "nope",
        ] {
            let input = GateInput {
                base_sha: None,
                exports: Vec::new(),
                workroot: workroot.clone(),
                workdir: candidate.to_string(),
                checks: vec![named_check("card-check", "true", None)],
            };
            let e = do_gate(&tiny(), &input, &|| false).unwrap_err();
            assert_eq!(e.kind(), std::io::ErrorKind::InvalidInput, "{candidate}");
            let ActivityError::Application(f) = gate_error(e) else {
                panic!("expected an application failure for {candidate}")
            };
            assert!(f.is_non_retryable(), "{candidate}");
        }
        // run_check_cancellable was never entered: it would have created this
        // sibling home dir for the missing subdir.
        assert!(!root.join("nope.home-check").exists());

        // A subdir of the root is inside the jail and runs normally.
        let input = GateInput {
            base_sha: None,
            exports: Vec::new(),
            workroot,
            workdir: "sub".to_string(),
            checks: vec![named_check("card-check", "test -d . && echo ok", None)],
        };
        let r = run(&tiny(), &input);
        assert!(r.passed, "{}", r.checks[0].tail);
        assert_eq!(r.checks[0].tail, "ok");
    }

    /// Fake secrets are built at runtime from fragments so that no token-shaped
    /// literal ever appears in this file.
    fn fake_sk(filler: &str) -> String {
        ["sk", "-", filler].concat()
    }

    fn fake_env(filler: &str) -> String {
        ["API_", "KEY=", filler].concat()
    }

    #[test]
    fn gate_tail_is_redacted_before_it_is_returned_and_hashed() {
        let d = tempfile::tempdir().unwrap();
        let (sk_a, sk_b) = (fake_sk(&"a".repeat(24)), fake_sk(&"b".repeat(24)));
        let (env_a, env_b) = (fake_env("hunter22hunter22"), fake_env("sleeper33sleeper3"));
        let cmd_a = format!("echo {sk_a}; echo {env_a}; exit 1");
        let cmd_b = format!("echo {sk_b}; echo {env_b}; exit 1");

        let a = run(
            &tiny(),
            &gate_input(d.path(), vec![named_check("card-check", &cmd_a, None)]),
        );
        let b = run(
            &tiny(),
            &gate_input(d.path(), vec![named_check("card-check", &cmd_b, None)]),
        );
        let (ta, tb) = (&a.checks[0].tail, &b.checks[0].tail);
        for secret in [sk_a.as_str(), sk_b.as_str(), env_a.as_str(), env_b.as_str()] {
            assert!(!ta.contains(secret) && !tb.contains(secret), "{ta}");
        }
        assert!(ta.contains(redact::REDACTED) && tb.contains(redact::REDACTED));
        // The hash is over the redacted tail, so two runs that differed only in
        // the secret compare equal — and the hash proves nothing about a guess.
        assert_eq!(ta, tb);
        assert_eq!(a.checks[0].output_hash, b.checks[0].output_hash);
        assert_eq!(a.checks[0].output_hash, hash::hash_bytes(ta.as_bytes()));
        assert_eq!(b.checks[0].output_hash, hash::hash_bytes(tb.as_bytes()));
    }

    #[test]
    fn gate_tail_is_capped_in_lines_and_bytes() {
        let cfg = Config {
            check_tail_lines: 50,
            ..tiny()
        };
        let d = tempfile::tempdir().unwrap();
        let r = run(
            &cfg,
            &gate_input(d.path(), vec![named_check("card-check", "seq 1 500", None)]),
        );
        let lines: Vec<&str> = r.checks[0].tail.lines().collect();
        assert_eq!(lines.len(), 50);
        assert_eq!(lines[0], "451");
        assert_eq!(lines[49], "500");

        let r = run(
            &cfg,
            &gate_input(
                d.path(),
                vec![named_check(
                    "card-check",
                    "head -c 200000 /dev/zero | tr '\\0' 'a'; echo",
                    None,
                )],
            ),
        );
        assert!(
            r.checks[0].tail.len() <= redact::MAX_TAIL_BYTES,
            "{}",
            r.checks[0].tail.len()
        );
    }

    /// True if Linux still exposes this pid as a live (non-zombie) process.
    fn pid_alive(pid: &str) -> bool {
        std::fs::read_to_string(format!("/proc/{pid}/stat")).is_ok_and(|s| {
            s.rsplit(')')
                .next()
                .and_then(|t| t.split_whitespace().next())
                != Some("Z")
        })
    }
