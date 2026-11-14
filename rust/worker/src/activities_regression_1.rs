    use super::*;
    use crate::harness::{Harness, HarnessRun};
    use crate::keeper::StubKeeper;
    use pg_worker_core::config::Tier;
    use pg_worker_core::worktree;
    use std::process::{Command, Stdio};
    use tempfile::TempDir;

    use crate::keeper::KeeperError;

    use park_test_support::{g3_exec, g3_live, g3_packet};

    mod park_test_support {
        include!("activities_park_test_support.rs");
    }

    /// G3-1 + V7-5: the park path presents exactly the observation the
    /// workflow recorded in the input and never re-reads the keeper.
    #[test]
    fn park_forwards_the_recorded_observation_without_reading() {
        let input = park_json_with_attempt(&g3_packet(), Some(4), Some(vec!["waiting".into()]));
        // What the activity forwards (the whole of the park path's state use).
        assert_eq!(expected_state_of(&input), Some(vec!["waiting".to_string()]));
        let keeper = StubKeeper::default();
        // What the projector holds is what the workflow observed at decision
        // time; the park must present it without asking the keeper again.
        keeper.set_card_state(Some("waiting"));
        keeper
            .park(&g3_packet(), &g3_exec(expected_state_of(&input)))
            .expect("park with its recorded observation lands");
        assert!(
            keeper.card_state_calls().is_empty(),
            "G3-1: the park path re-read the keeper"
        );
        assert_eq!(
            keeper
                .execs()
                .last()
                .expect("park was called")
                .expected_state,
            Some(vec!["waiting".to_string()])
        );
        // No observation recorded: the workflow's live-set fallback stands.
        let plain = park_json_with_attempt(&g3_packet(), Some(4), Some(g3_live()));
        assert_eq!(expected_state_of(&plain), Some(g3_live()));
        assert_eq!(
            expected_state_of(&park_json_with_attempt(&g3_packet(), Some(4), None)),
            None
        );
    }

    /// G3-1 (red): a retried park carries the observation from decision time,
    /// so once the projector moved the card on, the keeper path refuses it
    /// (stub equivalent of `exec_stale_state`) instead of re-reading a fresher
    /// state that would make the CAS tautological.
    #[test]
    fn a_retried_park_with_a_stale_observation_is_refused() {
        let keeper = StubKeeper::default();
        // The card the run parked on was `running` when it decided; the
        // projector wrote `waiting` while the activity was delayed.
        keeper.set_card_state(Some("waiting"));
        let input = park_json_with_attempt(&g3_packet(), Some(4), Some(vec!["running".into()]));
        let err = keeper
            .park(&g3_packet(), &g3_exec(expected_state_of(&input)))
            .expect_err("stale observation must be refused");
        assert_eq!(err, KeeperError::Conflict("exec_stale_state".to_string()));
        assert!(keeper.parked().is_empty());
        assert!(
            keeper.card_state_calls().is_empty(),
            "G3-1: a retry must not re-read to repair its expectation"
        );
        // The same park with the observation that still holds lands.
        keeper.set_card_state(Some("running"));
        keeper
            .park(&g3_packet(), &g3_exec(expected_state_of(&input)))
            .expect("fresh observation lands");
    }

    /// V7-5: the `park` activity's own code path -- [`park_path`], everything
    /// below the context binding the activity does -- forwards the observation
    /// it passes and never reads the keeper's card state (G3-1), on the
    /// landing case and on the refused one.
    #[tokio::test]
    async fn the_park_activity_path_presents_the_observation_it_passes() {
        let stub = Arc::new(StubKeeper::default());
        stub.set_card_state(Some("waiting"));
        let keeper: Arc<dyn Keeper> = stub.clone();
        // The identity the activity builds: observation verbatim from input.
        let landed = park_json_with_attempt(&g3_packet(), Some(4), Some(vec!["waiting".into()]));
        let exec = g3_exec(expected_state_of(&landed));
        park_path(keeper.clone(), landed, exec)
            .await
            .expect("park with its recorded observation lands");
        assert_eq!(stub.parked().len(), 1);
        assert!(
            stub.card_state_calls().is_empty(),
            "V7-5/G3-1: the park path read the keeper"
        );
        // The refused case runs the same code: stale observation, no re-read.
        let stale = park_json_with_attempt(&g3_packet(), Some(4), Some(vec!["running".into()]));
        let exec = g3_exec(expected_state_of(&stale));
        assert!(park_path(keeper.clone(), stale, exec).await.is_err());
        assert_eq!(stub.parked().len(), 1, "the stale park did not land");
        assert!(stub.card_state_calls().is_empty());
    }

    #[tokio::test]
    async fn park_path_join_error_preserves_panic_cause() {
        let stub = Arc::new(StubKeeper::default());
        stub.set_park_panic(Some("keeper exploded in park"));
        let keeper: Arc<dyn Keeper> = stub;
        let input = park_json_with_attempt(&g3_packet(), Some(4), Some(vec!["waiting".into()]));
        let exec = g3_exec(expected_state_of(&input));
        let err = park_path(keeper, input, exec).await.unwrap_err();
        assert!(
            format!("{err:?}").contains("keeper exploded in park"),
            "JoinError must retain the panic cause, got: {err:?}"
        );
    }

    /// R4 (red): a 409 with some other keeper code (`lease_held`, `exec_stale`)
    /// is not the CAS miss: untyped, so the re-observe loop must never fire.
    #[tokio::test]
    async fn park_409_with_another_code_is_not_typed_stale() {
        for code in ["lease_held", "exec_stale"] {
            let stub = Arc::new(StubKeeper::default());
            stub.set_park_error(Some(KeeperError::Conflict(code.to_string())));
            let keeper: Arc<dyn Keeper> = stub.clone();
            let input = park_json_with_attempt(&g3_packet(), Some(4), Some(vec!["waiting".into()]));
            let exec = g3_exec(expected_state_of(&input));
            let err = park_path(keeper, input, exec)
                .await
                .expect_err("a refused park must fail");
            let ActivityError::Application(f) = err else {
                panic!("a refused park must be an application failure");
            };
            assert!(f.is_non_retryable(), "{code}");
            assert_eq!(f.type_name(), None, "{code}: non-CAS 409 must not be typed");
            assert_eq!(stub.park_execs().len(), 1, "{code}: one park exec");
            assert!(stub.card_state_calls().is_empty(), "{code}: re-observed");
        }
    }

    /// D2/D3/R4: only a park 409 carrying `exec_stale_state` is typed so the
    /// workflow can re-observe; every other refusal stays non-retryable but
    /// untyped, and a transient 5xx keeps the retry policy's default.
    #[test]
    fn only_a_stale_park_refusal_is_typed_and_a_5xx_stays_retryable() {
        for code in [401, 403, 422] {
            let ActivityError::Application(f) = keeper_park_error(KeeperError::Status(code)) else {
                panic!("a refused park must be an application failure");
            };
            assert!(f.is_non_retryable(), "{code}");
            assert_eq!(f.type_name(), None, "{code}");
        }
        // R4: only `exec_stale_state` is the CAS miss; other 409s are untyped.
        for err in [
            KeeperError::Status(409),
            KeeperError::Conflict("lease_held".to_string()),
            KeeperError::Conflict(String::new()),
        ] {
            let ActivityError::Application(f) = keeper_park_error(err.clone()) else {
                panic!("a refused park must be an application failure");
            };
            assert!(f.is_non_retryable(), "{err:?}");
            assert_eq!(f.type_name(), None, "{err:?}");
        }
        let err = KeeperError::Conflict(crate::keeper::STALE_STATE_FAILURE.to_string());
        let ActivityError::Application(f) = keeper_park_error(err) else {
            panic!("a stale park must be an application failure");
        };
        assert!(f.is_non_retryable());
        assert_eq!(f.type_name(), Some(crate::keeper::STALE_STATE_FAILURE));
        let ActivityError::Application(f) = keeper_park_error(KeeperError::Status(500)) else {
            panic!("a transient park failure must be an application failure");
        };
        assert!(!f.is_non_retryable());
        assert_eq!(f.type_name(), None);
    }

    /// G3-3: an ask keeps the any-of live-set CAS the ADR documents -- it is
    /// never narrowed to a single state -- and re-reads nothing (G3-1).
    #[test]
    fn ask_keeps_the_any_of_live_set_and_does_not_read() {
        let keeper = StubKeeper::default();
        keeper.set_card_state(Some("running"));
        let input = AskInput {
            expected_state: Some(g3_live()),
            card: "c-1".into(),
            question: Question {
                text: "which db?".into(),
                default: Some("sqlite".into()),
                deadline_s: Some(60),
                risk_tags: Vec::new(),
            },
            attempt: Some(4),
        };
        // Exactly what the activity sends: the input set, verbatim.
        let exec = g3_exec(input.expected_state.clone());
        keeper
            .ask_question(&input.card, &input.question, &exec)
            .expect("ask lands");
        assert_eq!(
            keeper
                .execs()
                .last()
                .expect("ask was called")
                .expected_state,
            Some(g3_live())
        );
        assert!(keeper.card_state_calls().is_empty());
    }

    /// G3-4: the one keeper read left waits a bounded time off the runtime
    /// threads, and a stalled keeper is a transient failure to retry (D1b),
    /// never a silent absence.
    #[tokio::test]
    async fn observe_read_is_bounded_by_the_five_second_cap() {
        assert!(
            OBSERVE_READ_TIMEOUT <= Duration::from_secs(5),
            "G3-4: the read timeout must stay <= 5 s"
        );
        let stub = Arc::new(StubKeeper::default());
        stub.set_card_state(Some("waiting"));
        stub.set_card_state_delay(Some(Duration::from_millis(500)));
        let read: Arc<dyn Keeper> = stub.clone();
        let started = Instant::now();
        assert!(
            observe_read_within(read, "c-1".into(), Duration::from_millis(25))
                .await
                .is_err(),
            "a stalled read is a transient failure to retry"
        );
        assert!(started.elapsed() < Duration::from_secs(5));
        // An unstalled read hands the observation over.
        stub.set_card_state_delay(None);
        assert_eq!(
            observe_read(stub.clone(), "c-1".into()).await.ok(),
            Some(Some("waiting".to_string()))
        );
        assert_eq!(
            stub.card_state_calls(),
            vec!["c-1".to_string(), "c-1".to_string()]
        );
    }

    #[test]
    fn keeper_failures_are_classified() {
        for code in [401, 403, 409, 422] {
            assert_eq!(classify(&KeeperError::Status(code)), Failure::Refused);
        }
        for code in [408, 429, 500, 502, 503, 599] {
            assert_eq!(classify(&KeeperError::Status(code)), Failure::Transient);
        }
        assert_eq!(classify(&KeeperError::Http("x".into())), Failure::Transient);
        for code in [400, 404, 410] {
            assert_eq!(classify(&KeeperError::Status(code)), Failure::Other);
        }
        assert_eq!(classify(&KeeperError::Decode("x".into())), Failure::Other);
    }

    #[test]
    fn transient_failures_are_retried_then_succeed() {
        let mut calls = 0;
        let r = retry_transient(&[Duration::ZERO; 3], || {
            calls += 1;
            if calls < 3 {
                Err(KeeperError::Status(429))
            } else {
                Ok(7)
            }
        });
        assert_eq!((r, calls), (Ok(7), 3));
    }

    #[test]
    fn transient_failures_are_bounded_and_permanent_ones_are_not_retried() {
        let mut calls = 0;
        let r: Result<(), _> = retry_transient(&[Duration::ZERO; 3], || {
            calls += 1;
            Err(KeeperError::Status(503))
        });
        assert_eq!((r, calls), (Err(KeeperError::Status(503)), 4));
        for code in [401, 403, 409, 422, 400] {
            let mut calls = 0;
            let r: Result<(), _> = retry_transient(&[Duration::ZERO; 3], || {
                calls += 1;
                Err(KeeperError::Status(code))
            });
            assert_eq!((r, calls), (Err(KeeperError::Status(code)), 1), "{code}");
        }
    }
