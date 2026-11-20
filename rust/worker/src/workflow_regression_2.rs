    #[test]
    fn merge_time_failures_carry_main_head_as_the_new_base() {
        for o in ["check_fail", "contract_defect", "merge_refused"] {
            match map_merge_result(merge_result_of(o)) {
                MergeStep::Failed(_, _, h) => assert_eq!(h, "h", "{o}"),
                other => panic!("{o}: {other:?}"),
            }
        }
        let mut base = "old".to_string();
        adopt_base(&mut base, "new".to_string());
        assert_eq!(base, "new");
        adopt_base(&mut base, String::new());
        assert_eq!(base, "new", "an empty head keeps the base");
    }

    fn gate_check_result(name: &str, passed: bool, timed_out: bool, tail: &str) -> GateCheckResult {
        GateCheckResult {
            name: name.to_string(),
            passed,
            exit_code: None,
            signal: None,
            timed_out,
            tail: tail.to_string(),
            output_hash: String::new(),
        }
    }

    fn gate_result(passed: bool, checks: Vec<GateCheckResult>) -> GateResult {
        GateResult { passed, checks }
    }

    #[test]
    fn gate_checks_keeps_builtins_for_a_blank_check_cmd() {
        for blank in ["", " ", "\t\n  "] {
            let (_root, mut input) = card_input(vec![leg("leg-1", Tier::Free)]);
            input.claim.check_cmd = blank.to_string();
            let checks = gate_checks(&input);
            assert_eq!(checks.len(), 2, "{blank:?}");
            assert_eq!(checks[0].builtin.as_deref(), Some("diffscan"));
            assert_eq!(checks[1].builtin.as_deref(), Some("exports"));
        }
    }

    #[test]
    fn gate_checks_names_one_card_check_with_the_untrimmed_cmd() {
        let (_root, mut input) = card_input(vec![leg("leg-1", Tier::Free)]);
        input.claim.check_cmd = "  cargo test  ".to_string();
        let checks = gate_checks(&input);
        assert_eq!(checks.len(), 3);
        assert_eq!(checks[0].name, "check");
        // The original string is kept, not a trimmed copy: the shell owns
        // whitespace.
        assert_eq!(checks[0].cmd, "  cargo test  ");
        // Unset: the activity shares its `start_to_close` budget across checks.
        assert_eq!(checks[0].timeout_s, None);
    }

    #[test]
    fn map_gate_result_is_none_only_when_the_gate_passed() {
        assert!(map_gate_result(gate_result(true, vec![]), "base").is_none());
        // `passed` is the authority: a stale nonzero exit code does not block.
        assert!(
            map_gate_result(
                gate_result(
                    true,
                    vec![gate_check_result("card-check", true, false, "boom")]
                ),
                "base"
            )
            .is_none()
        );
    }

    #[test]
    fn map_gate_result_blocks_a_failure_with_check_fail() {
        let step = map_gate_result(
            gate_result(
                false,
                vec![gate_check_result("card-check", false, false, "boom")],
            ),
            "base",
        );
        match step {
            Some(MergeStep::Failed(ExitClass::CheckFail, tail, base)) => {
                assert_eq!(tail, "[card-check]\nboom");
                // A blocked gate leaves main alone, so the base is main's head.
                assert_eq!(base, "base");
            }
            other => panic!("a failed gate must block the merge, got {other:?}"),
        }
    }

    #[test]
    fn map_gate_result_marks_a_timeout_in_the_tail() {
        let step = map_gate_result(
            gate_result(
                false,
                vec![gate_check_result(
                    "card-check",
                    false,
                    true,
                    "partial output",
                )],
            ),
            "base",
        );
        let Some(MergeStep::Failed(ExitClass::CheckFail, tail, base)) = step else {
            panic!("a timed-out gate must block the merge");
        };
        assert!(tail.contains("[card-check]"), "{tail}");
        assert!(tail.contains("timed out\npartial output"), "{tail}");
        assert_eq!(base, "base");
    }

    #[test]
    fn map_gate_result_names_each_failing_check_and_omits_passing_ones() {
        let r = gate_result(
            false,
            vec![
                gate_check_result("unit", false, false, "unit broke"),
                gate_check_result("diff-scan", true, false, "clean"),
                gate_check_result("lint", false, true, "lint output"),
            ],
        );
        match map_gate_result(r, "base") {
            Some(MergeStep::Failed(ExitClass::CheckFail, tail, _)) => {
                assert!(tail.contains("[unit]\nunit broke"), "{tail}");
                assert!(tail.contains("[lint]\ntimed out\nlint output"), "{tail}");
                // The failing blocks are joined by a blank line...
                assert!(tail.contains("unit broke\n\n[lint]"), "{tail}");
                // ...and the passing check is not named anywhere.
                assert!(
                    !tail.contains("diff-scan") && !tail.contains("clean"),
                    "{tail}"
                );
            }
            other => panic!("a failed gate must block the merge, got {other:?}"),
        }
    }

    #[test]
    fn map_gate_result_tail_is_capped_keeping_the_end() {
        let big = "x".repeat(redact::MAX_TAIL_BYTES + 1000);
        let step = map_gate_result(
            gate_result(
                false,
                vec![gate_check_result("card-check", false, false, &big)],
            ),
            "base",
        );
        let Some(MergeStep::Failed(ExitClass::CheckFail, tail, _)) = step else {
            panic!("a failed gate must block the merge");
        };
        assert!(tail.len() <= redact::MAX_TAIL_BYTES, "{}", tail.len());
        // The cap keeps the END: what is kept is a suffix of the check's tail.
        assert!(big.ends_with(&tail), "tail is not a suffix of the source");
    }

    #[test]
    fn gate_options_are_bounded() {
        let cfg = Config::allow_all_for_tests();
        let opts = gate_options(&cfg);
        assert_eq!(
            opts.close_timeouts.start_to_close(),
            Some(cfg.start_to_close)
        );
        assert_eq!(opts.heartbeat_timeout, Some(cfg.heartbeat_timeout));
        let retry = opts
            .retry_policy
            .expect("the gate options carry a retry policy");
        assert_eq!(retry.maximum_attempts(), 3);
        assert_eq!(retry.initial_interval(), Duration::from_secs(2));
    }

    #[test]
    fn gate_patch_id_is_stable() {
        // Renaming the marker would strand every card whose history already
        // carries it, so the id is pinned.
        assert_eq!(GATE_PATCH, "gate-before-merge-v1");
    }

    #[test]
    fn park_reobserve_patch_id_is_stable() {
        // Renaming the marker would strand every card whose history already
        // carries it, so the id is pinned.
        assert_eq!(PARK_REOBSERVE_PATCH, "park-reobserve-v1");
    }

    /// D2: the typed park refusal is recognised anywhere along a failure's
    /// cause chain -- an activity failure wraps the application failure the
    /// `park` activity returned as its cause -- and nothing else matches.
    #[test]
    fn stale_state_refusal_is_found_along_the_cause_chain() {
        let typed = Failure {
            message: "keeper returned unexpected status 409".into(),
            failure_info: Some(FailureInfo::ApplicationFailureInfo(
                ApplicationFailureInfo {
                    r#type: STALE_STATE_FAILURE.into(),
                    ..Default::default()
                },
            )),
            ..Default::default()
        };
        assert!(is_stale_state_refusal(Some(&typed)));
        // The shape the workflow actually sees: "Activity task failed" with the
        // typed refusal as its cause.
        let wrapped = Failure {
            message: "Activity task failed".into(),
            cause: Some(Box::new(typed)),
            ..Default::default()
        };
        assert!(is_stale_state_refusal(Some(&wrapped)));
        // An untyped application failure, a failure with no info and no
        // failure at all are not stale refusals.
        let other = Failure {
            message: "keeper refused".into(),
            failure_info: Some(FailureInfo::ApplicationFailureInfo(
                ApplicationFailureInfo::default(),
            )),
            ..Default::default()
        };
        assert!(!is_stale_state_refusal(Some(&other)));
        assert!(!is_stale_state_refusal(Some(&Failure::default())));
        assert!(!is_stale_state_refusal(None));
    }

    #[test]
    fn a_merge_time_verdict_replaces_the_attempts_entry_not_adds_one() {
        // Attempt 1 ran ok (entry pushed), then failed its post-rebase check.
        let mut history = vec![attempt(ExitClass::Ok)];
        replace_attempt(&mut history, attempt(ExitClass::CheckFail));
        assert_eq!(history.len(), 1);
        assert_eq!(history[0].exit_class, ExitClass::CheckFail);
        // Only the judged entry consumes budget: one attempt, not two.
        assert_eq!(history.iter().filter(|a| a.consumes_attempt()).count(), 1);
        // An earlier attempt's entry is untouched.
        let mut two = vec![attempt(ExitClass::CheckFail), attempt(ExitClass::Ok)];
        replace_attempt(&mut two, attempt(ExitClass::RebaseConflict));
        assert_eq!(two.len(), 2);
        assert_eq!(two[0].exit_class, ExitClass::CheckFail);
        assert_eq!(two[1].exit_class, ExitClass::RebaseConflict);
    }

    #[test]
    fn a_merge_time_verdict_is_re_reported_with_the_same_attempt_number() {
        // The keeper first saw attempt 3 as `ok`; the merge-time check failed.
        let r = judged_report("c-1", 3, "leg-a", ExitClass::CheckFail, "dh", "tail");
        assert_eq!((r.card.as_str(), r.n, r.leg.as_str()), ("c-1", 3, "leg-a"));
        assert_eq!(r.exit_class, ExitClass::CheckFail);
        assert_eq!(
            (r.diff_hash.as_str(), r.check_tail.as_str()),
            ("dh", "tail")
        );
    }

    #[test]
    fn retry_later_cap_parks_with_merge_refused() {
        assert!(!merge_retry_exhausted(0));
        assert!(!merge_retry_exhausted(MERGE_RETRY_CAP));
        assert!(merge_retry_exhausted(MERGE_RETRY_CAP + 1));
        // The park packet the loop builds for it.
        let p = packet_for(
            "c-1",
            Tier::Free,
            ExitClass::MergeRefused,
            MERGE_LOCK_PARK_REASON,
        );
        assert_eq!(p.exit_class, ExitClass::MergeRefused);
        assert_eq!(p.check_tail, MERGE_LOCK_PARK_REASON);
        assert!(matches!(
            map_merge_result(merge_result_of("retry_later")),
            MergeStep::RetryLater
        ));
    }

    #[test]
    fn merge_activity_is_single_attempt() {
        let cfg = Config::allow_all_for_tests();
        let retry = once_options(&cfg)
            .retry_policy
            .expect("merge options carry a retry policy");
        assert_eq!(retry.maximum_attempts(), 1);
    }

    #[test]
    fn janitor_is_detached_from_workflow_cancellation() {
        let cfg = Config::allow_all_for_tests();
        let token = janitor_options(&cfg)
            .cancellation_token
            .expect("the janitor names its own cancellation token");
        // A detached token never observes the workflow's cancellation, so the
        // sweep still runs to completion after a native cancel.
        assert!(!token.is_cancelled());
    }

    #[test]
    fn finish_maps_every_detail_to_a_projector_outcome() {
        // docs/projector.md contract v1: the workflow result is done | parked | failed.
        let cases = [
            ("merged", "done"),
            ("parked", "parked"),
            ("planner", "parked"),
            ("cancelled", "parked"),
            ("quarantined", "failed"),
            ("something_else", "failed"),
        ];
        for (detail, outcome) in cases {
            let out = finish(detail, None, 3);
            assert_eq!(out.detail, detail, "detail is carried verbatim");
            assert_eq!(out.outcome, outcome, "detail {detail}");
            assert_eq!(out.merged_sha, None);
            assert_eq!(out.attempts, 3);
        }
    }

    #[test]
    fn finish_keeps_the_merged_sha() {
        let out = finish("merged", Some("sha".to_string()), 7);
        assert_eq!(out.outcome, "done");
        assert_eq!(out.detail, "merged");
        assert_eq!(out.merged_sha.as_deref(), Some("sha"));
        assert_eq!(out.attempts, 7);
    }

    #[test]
    fn context_class_names_match_the_projector_wire_form() {
        assert_eq!(context_class_name(ContextClass::K128), "128k");
        assert_eq!(context_class_name(ContextClass::K256), "256k");
        assert_eq!(context_class_name(ContextClass::M1), "1m");
    }
