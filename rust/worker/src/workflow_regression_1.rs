    use super::*;
    use crate::activities::GateCheckResult;
    use tempfile::TempDir;
    use temporalio_common::protos::temporal::api::failure::v1::ApplicationFailureInfo;

    fn leg(id: &str, tier: Tier) -> LegSpec {
        LegSpec {
            id: id.to_string(),
            tier,
            model: "some/model".to_string(),
            harness: "opencode".to_string(),
        }
    }

    fn attempt(exit_class: ExitClass) -> Attempt {
        Attempt {
            exit_class,
            leg: "leg-1".to_string(),
            tier: Tier::Free,
            diff_hash: String::new(),
            check_hash: String::new(),
            context_overflow: false,
        }
    }

    /// A card input whose workroot is a real temporary directory.
    fn card_input(legs: Vec<LegSpec>) -> (TempDir, CardInput) {
        let root = tempfile::tempdir().unwrap();
        let claim = Claim {
            card: "c-1".to_string(),
            repo: "org/repo".to_string(),
            base_sha: "abc123".to_string(),
            check_cmd: "true".to_string(),
            allow: vec!["src/**".to_string()],
            forbid: Vec::new(),
            prompt: "do the thing".to_string(),
            ..Default::default()
        };
        let input = CardInput {
            legacy: crate::activities::CardInput {
                claim,
                legs,
                workroot: root.path().to_string_lossy().into_owned(),
                main_branch: "main".to_string(),
                question_wait_cap_s: None,
                max_question_rounds: None,
            },
            risk_tagged: false,
            heldout_cmd: String::new(),
            smoke_checks: Vec::new(),
            exports: Vec::new(),
            base_sha: None,
            resume: None,
        };
        (root, input)
    }

    #[test]
    fn old_card_json_defaults_gate_metadata() {
        let old = serde_json::json!({
            "claim": {"card":"c", "repo":"r", "base_sha":"abc", "check_cmd":"true",
                "allow":[], "forbid":[], "prompt":"p"},
            "legs":[], "workroot":"work", "main_branch":"main"
        });
        let input: CardInput = serde_json::from_value(old).unwrap();
        assert!(!input.risk_tagged);
        assert!(input.heldout_cmd.is_empty());
        assert!(input.smoke_checks.is_empty());
        assert!(input.exports.is_empty());
        assert!(input.base_sha.is_none());
    }

    #[test]
    fn gate_diff_uses_current_attempt_base_not_stale_wire_base() {
        let (_root, mut input) = card_input(vec![]);
        input.base_sha = Some("stale-wire-base".into());
        let gate = gate_input(&input, "current-attempt-base", 2, gate_checks(&input));
        assert_eq!(gate.base_sha.as_deref(), Some("current-attempt-base"));
    }

    #[test]
    fn gate_checks_compose_commands_and_builtins() {
        let (_root, mut input) = card_input(vec![]);
        input.risk_tagged = true;
        input.heldout_cmd = "  heldout  ".into();
        input.smoke_checks = vec!["smoke one".into(), "smoke two".into()];
        input.exports = vec!["src/a:run".into()];
        input.base_sha = Some("explicit-base".into());
        let checks = gate_checks(&input);
        assert_eq!(
            checks.iter().map(|c| c.name.as_str()).collect::<Vec<_>>(),
            vec![
                "check", "heldout", "smoke-1", "smoke-2", "diffscan", "exports"
            ]
        );
        assert_eq!(checks[1].cmd, "  heldout  ");
        assert_eq!(checks[2].cmd, "smoke one");
        assert_eq!(checks[3].cmd, "smoke two");
        assert!(checks[..4].iter().all(|c| c.builtin.is_none()));
        assert_eq!(checks[4].builtin.as_deref(), Some("diffscan"));
        assert_eq!(checks[5].builtin.as_deref(), Some("exports"));
        assert!(checks.iter().all(|c| c.timeout_s.is_none()));
    }

    #[test]
    fn risk_tagged_card_cannot_skip_missing_heldout() {
        let (_root, mut input) = card_input(vec![]);
        input.risk_tagged = true;
        let checks = gate_checks(&input);
        let heldout = checks.iter().find(|c| c.name == "heldout").unwrap();
        assert_eq!(heldout.cmd, "false");
        assert!(heldout.builtin.is_none());
    }

    #[test]
    fn answer_signal_tolerates_extra_and_missing_optional_fields() {
        let a: AnswerSignal = serde_json::from_value(serde_json::json!({
            "question_id": 7, "text": "yes", "extra": {"a": 1}, "more": null,
        }))
        .unwrap();
        assert_eq!(
            a,
            AnswerSignal {
                question_id: 7,
                key: String::new(),
                text: "yes".into(),
                source: String::new(),
            }
        );
        let full: AnswerSignal = serde_json::from_value(serde_json::json!({
            "question_id": "12", "key": "k", "text": "t", "source": "answer",
        }))
        .unwrap();
        assert_eq!((full.question_id, full.key.as_str()), (12, "k"));
    }

    /// sq5 fix 4 (deepseek, D-2410 HIGH): the real complete activity carries the
    /// live set the worker observed instead of `None`, so keeper `_worker_complete`
    /// gets a real CAS instead of "whatever the card happens to be now".
    #[test]
    fn complete_sends_the_live_set_as_expected_state() {
        let input = done_input("c-1".to_string(), "deadbeef".to_string(), 3);
        let states = input
            .expected_state
            .expect("complete must carry the observed live set, not None");
        assert_eq!(
            states,
            WORKER_LIVE_STATES
                .iter()
                .map(|s| (*s).to_owned())
                .collect::<Vec<_>>()
        );
        // The merge-retry/probe paths this CAS has to cover live in waiting and
        // needs_input, so both must be part of what complete accepts.
        assert!(states.iter().any(|s| s == "waiting"));
        assert!(states.iter().any(|s| s == "needs_input"));
        assert_eq!(
            (
                input.card.as_str(),
                input.merged_sha.as_str(),
                input.attempt
            ),
            ("c-1", "deadbeef", Some(3))
        );
    }

    #[test]
    fn an_answer_for_another_question_is_not_consumed() {
        let mut card = Card::default();
        card.accept_answer(serde_json::json!({"question_id": 2, "text": "for two"}));
        assert_eq!(card.answer_text(1), None);
        card.accept_answer(serde_json::json!({"question_id": 1, "text": "for one"}));
        assert_eq!(card.answer_text(1).as_deref(), Some("for one"));
        // The other one is still there for its own question.
        assert_eq!(card.answer_text(2).as_deref(), Some("for two"));
    }

    #[test]
    fn unparsable_answers_are_ignored_not_fatal() {
        let mut card = Card::default();
        for bad in [
            serde_json::json!("text"),
            serde_json::json!(null),
            serde_json::json!({"text": "no id"}),
            serde_json::json!({"question_id": 1}),
            serde_json::json!({"question_id": 1.5, "text": "t"}),
            serde_json::json!({"question_id": [1], "text": "t"}),
            serde_json::json!({"question_id": 1, "text": 9}),
        ] {
            card.accept_answer(bad);
        }
        assert!(card.answers.is_empty());
        // A question_timeout signal is stored but is never an answer.
        card.accept_answer(
            serde_json::json!({"question_id": 3, "text": "d", "source": "question_timeout"}),
        );
        assert_eq!(card.answer_text(3), None);
    }

    #[test]
    fn stored_answers_are_bounded() {
        let mut card = Card::default();
        for i in 0..(MAX_STORED_ANSWERS as i64 + 10) {
            card.accept_answer(serde_json::json!({"question_id": i, "text": "t"}));
        }
        assert_eq!(card.answers.len(), MAX_STORED_ANSWERS);
        // The newest are kept, the oldest dropped.
        assert_eq!(
            card.answers.last().unwrap().question_id,
            MAX_STORED_ANSWERS as i64 + 9
        );
        assert_eq!(card.answers[0].question_id, 10);
    }

    #[test]
    fn stored_answer_text_is_capped() {
        let mut card = Card::default();
        let long = "x".repeat(question::MAX_TEXT_CHARS + 500);
        card.accept_answer(serde_json::json!({"question_id": 1, "text": long}));
        let got = card.answer_text(1).unwrap();
        assert!(got.ends_with(question::TRUNCATION_MARKER));
        assert_eq!(
            got.chars().count(),
            question::MAX_TEXT_CHARS + question::TRUNCATION_MARKER.chars().count()
        );
    }

    #[test]
    fn legs_for_marks_limited_legs() {
        let (_root, input) = card_input(vec![leg("a", Tier::Free), leg("b", Tier::Paid)]);
        let legs = legs_for(&input, &["b".to_string()]);
        assert_eq!(
            legs,
            vec![
                Leg {
                    id: "a".to_string(),
                    tier: Tier::Free,
                    harness: "opencode".to_string(),
                    limited: false,
                },
                Leg {
                    id: "b".to_string(),
                    tier: Tier::Paid,
                    harness: "opencode".to_string(),
                    limited: true,
                },
            ]
        );
    }

    #[test]
    fn probe_fail_streak_counts_only_the_trailing_run() {
        assert_eq!(probe_fail_streak(&[]), 0);
        let history = vec![
            attempt(ExitClass::ProbeFail),
            attempt(ExitClass::CheckFail),
            attempt(ExitClass::ProbeFail),
            attempt(ExitClass::ProbeFail),
        ];
        assert_eq!(probe_fail_streak(&history), 2);
    }

    #[test]
    fn wait_duration_saturates_when_the_deadline_has_passed() {
        let now = UNIX_EPOCH + Duration::from_secs(100);
        assert_eq!(
            wait_duration(now + Duration::from_secs(5), now),
            Duration::from_secs(5)
        );
        assert_eq!(
            wait_duration(now - Duration::from_secs(5), now),
            Duration::ZERO
        );
        assert_eq!(wait_duration(now, now), Duration::ZERO);
    }

    #[test]
    fn auxiliary_activities_get_bounded_retries() {
        let cfg = Config::allow_all_for_tests();
        let retry = activity_options(&cfg)
            .retry_policy
            .expect("auxiliary options carry a retry policy");
        assert_eq!(retry.maximum_attempts(), 5);
        assert_eq!(retry.initial_interval(), Duration::from_secs(2));
    }

    fn merge_result_of(outcome: &str) -> MergeResult {
        MergeResult {
            outcome: outcome.to_string(),
            sha: Some("s".to_string()),
            main_head: "h".to_string(),
            detail: "d".to_string(),
        }
    }

    #[test]
    fn unknown_merge_outcome_is_refused_not_moved() {
        for o in ["", "moved", "MERGED", "rebase_conflict", "something_new"] {
            match map_merge_result(merge_result_of(o)) {
                MergeStep::Failed(ExitClass::MergeRefused, d, h) => {
                    assert_eq!(h, "h");
                    assert!(d.contains("unknown merge outcome"), "{o}: {d}")
                }
                other => panic!("{o:?} mapped to {other:?}"),
            }
        }
        assert!(matches!(
            map_merge_result(merge_result_of("base_moved")),
            MergeStep::Moved(h) if h == "h"
        ));
        assert!(matches!(
            map_merge_result(merge_result_of("conflict")),
            MergeStep::Moved(_)
        ));
        assert!(matches!(
            map_merge_result(merge_result_of("merged")),
            MergeStep::Merged(s) if s == "s"
        ));
    }
