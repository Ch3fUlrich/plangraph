    #[test]
    fn only_a_permanent_refusal_settles_to_none() {
        for code in [401, 403, 409, 422] {
            assert!(matches!(
                settle_refusal::<i64>(Err(KeeperError::Status(code)), "v"),
                Ok(None)
            ));
        }
        for code in [408, 429, 500, 503, 400] {
            assert!(settle_refusal::<i64>(Err(KeeperError::Status(code)), "v").is_err());
        }
        assert!(matches!(settle_refusal(Ok(3), "v"), Ok(Some(3))));
    }

    /// A harness that is never run by these tests; it only satisfies `Deps`.
    struct Fake;

    impl Harness for Fake {
        fn name(&self) -> &str {
            "fake"
        }

        fn command(&self, _run: &HarnessRun) -> Command {
            Command::new("true")
        }
    }

    fn deps() -> Deps {
        Deps {
            keeper: Arc::new(StubKeeper::default()),
            harnesses: HarnessRegistry::new().with("opencode", Arc::new(Fake)),
            cfg: tiny(),
            patterns: Patterns::default(),
        }
    }

    // Exercise Activities::report itself: testing exec_of alone would not catch
    // replacing its call in report with ExecId::default(). No git fixture needed.
    #[temporalio_macros::workflow]
    #[derive(Default)]
    pub struct ReportIdentity;

    #[temporalio_macros::workflow_methods]
    impl ReportIdentity {
        #[run]
        pub async fn run(
            ctx: &mut temporalio_sdk::WorkflowContext<Self>,
            input: AttemptReport,
        ) -> temporalio_sdk::WorkflowResult<()> {
            ctx.execute_activity(
                CardActivities::report,
                input,
                temporalio_sdk::ActivityOptions::start_to_close_timeout(Duration::from_secs(10)),
            )
            .await?;
            Ok(())
        }
    }

    #[tokio::test]
    async fn report_carries_the_real_workflow_run_and_card_attempt() {
        use temporalio_client::{
            Client, ClientOptions, Connection, ConnectionOptions, Url, WorkflowFetchHistoryOptions,
            WorkflowGetResultOptions, WorkflowStartOptions,
        };
        use temporalio_sdk::{Runtime, Worker, WorkerOptions};
        let Ok(addr) = std::env::var("PG_TEMPORAL_ADDR") else {
            return;
        };
        if addr.is_empty() {
            return;
        }
        tokio::task::LocalSet::new().run_until(async move {
            let runtime = Runtime::from_current_tokio(Default::default()).expect("runtime");
            let conn = Connection::connect(ConnectionOptions::new(addr.parse::<Url>().unwrap()).build())
                .await.unwrap();
            let client = Client::new(conn, ClientOptions::new("default").build()).unwrap();
            let id = format!("report-identity-{}-{}", std::process::id(),
                std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos());
            let keeper = Arc::new(StubKeeper::default());
            let mut d = deps();
            d.keeper = keeper.clone();
            let opts = WorkerOptions::new(id.as_str())
                .register_workflow::<ReportIdentity>().unwrap()
                .register_activities(CardActivities::new(d)).build();
            let mut worker = Worker::new(&runtime, client.clone(), opts).unwrap();
            let polling = tokio::task::spawn_local(async move { worker.run().await });
            let report = AttemptReport {
                card: "identity-card".into(), n: 7, leg: "leg".into(),
                exit_class: pg_worker_core::exit_class::ExitClass::Ok,
                diff_hash: "hash".into(), check_tail: String::new(),
            };
            client.start_workflow(ReportIdentity::run, report.clone(),
                WorkflowStartOptions::new(id.as_str(), id.as_str()).build()).await.unwrap();
            let handle = client.get_workflow_handle::<ReportIdentity>(id.as_str());
            tokio::time::timeout(Duration::from_secs(30),
                handle.get_result(WorkflowGetResultOptions::default())).await.unwrap().unwrap();
            let bytes = handle.fetch_history(WorkflowFetchHistoryOptions::default()).to_json().await.unwrap();
            polling.abort();
            let history: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
            let run_id = history["events"][0]["attributes"]["WorkflowExecutionStartedEventAttributes"]
                ["first_execution_run_id"].as_str().expect("real server run id");
            assert!(!run_id.is_empty());
            assert_eq!(keeper.attempts(), vec![report]);
            assert_eq!(keeper.attempt_execs(), vec![ExecId {
                workflow_id: Some(id), run_id: Some(run_id.to_owned()),
                attempt: Some(7), expected_state: None,
            }]);
        }).await;
    }

    /// Defaults with the watchdogs shrunk so probes finish quickly.
    fn tiny() -> Config {
        Config {
            start_to_close: Duration::from_secs(10),
            check_tail_lines: 20,
            ..Config::allow_all_for_tests()
        }
    }

    fn claim() -> Claim {
        Claim {
            card: "c1".to_string(),
            repo: "/repo".to_string(),
            base_sha: "base".to_string(),
            check_cmd: "true".to_string(),
            allow: vec!["src/**".to_string()],
            forbid: vec![".env".to_string()],
            prompt: "do the thing".to_string(),
            ..Default::default()
        }
    }

    fn card_input() -> CardInput {
        CardInput {
            claim: claim(),
            legs: vec![LegSpec {
                id: "leg-1".to_string(),
                tier: Tier::Free,
                model: "some/model".to_string(),
                harness: "opencode".to_string(),
            }],
            workroot: "/w".to_string(),
            main_branch: "main".to_string(),
            question_wait_cap_s: None,
            max_question_rounds: None,
        }
    }

    fn round_trip<T>(v: &T)
    where
        T: Serialize + serde::de::DeserializeOwned + PartialEq + std::fmt::Debug,
    {
        let j = serde_json::to_string(v).unwrap();
        let back: T = serde_json::from_str(&j).unwrap();
        assert_eq!(&back, v);
    }

    #[test]
    fn serde_round_trip_of_wire_types() {
        round_trip(&card_input());
        round_trip(&CardOutput {
            outcome: "done".to_string(),
            detail: "merged".to_string(),
            merged_sha: Some("sha".to_string()),
            attempts: 2,
        });
        round_trip(&tick_now());
        round_trip(&ProbeInput {
            repo: "/r".to_string(),
            workroot: "/w".to_string(),
            card: "c1".to_string(),
            n: 1,
            base_sha: "b".to_string(),
            check_cmd: "true".to_string(),
        });
        round_trip(&ProbeResult {
            outcome: "already_done".to_string(),
            branch: "attempt/c1/1".to_string(),
        });
        round_trip(&MergeInput {
            repo: "/r".to_string(),
            main_branch: "main".to_string(),
            branch: "attempt/c1/1".to_string(),
            base_sha: "b".to_string(),
            worktree: "/w/c1-1".to_string(),
            check_cmd: Some("true".to_string()),
            confine: Some(ConfineSpec {
                allow: vec!["src/**".to_string()],
                forbid: vec![],
            }),
        });
        round_trip(&MergeResult {
            outcome: "merged".to_string(),
            sha: Some("sha".to_string()),
            main_head: "sha".to_string(),
            detail: String::new(),
        });
        round_trip(&DoneInput {
            expected_state: None,
            card: "c1".to_string(),
            merged_sha: "sha".to_string(),
            attempt: None,
        });
        round_trip(&JanitorInput {
            repo: "/r".to_string(),
            workroot: "/w".to_string(),
            check_cmd: "true".to_string(),
            base_sha: "b".to_string(),
            card: "c1".to_string(),
        });
        round_trip(&GateInput {
            base_sha: None,
            exports: Vec::new(),
            workroot: "/w".to_string(),
            workdir: "/w/c1-1".to_string(),
            checks: vec![GateCheck {
                builtin: None,
                name: "card-check".to_string(),
                cmd: "true".to_string(),
                timeout_s: Some(30),
            }],
        });
        round_trip(&GateResult {
            passed: false,
            checks: vec![GateCheckResult {
                name: "card-check".to_string(),
                passed: false,
                exit_code: Some(3),
                signal: None,
                timed_out: false,
                tail: "boom".to_string(),
                output_hash: "h".to_string(),
            }],
        });
    }

    /// An old producer's JSON predates `detail`; the missing field must default
    /// to empty rather than fail the read, so rolling upgrades stay compatible.
    #[test]
    fn card_output_without_detail_deserializes() {
        let old = r#"{"outcome":"done","merged_sha":"sha","attempts":1}"#;
        let out: CardOutput = serde_json::from_str(old).unwrap();
        assert_eq!(out.outcome, "done");
        assert_eq!(out.detail, "");
        assert_eq!(out.merged_sha.as_deref(), Some("sha"));
        assert_eq!(out.attempts, 1);
    }

    /// The park activity input carries the card attempt as an extra top-level
    /// key: `park_json_with_attempt` adds it, and `ParkPacket::from_json` still
    /// parses the result because it ignores unknown keys.
    #[test]
    fn park_json_with_attempt_carries_the_attempt_and_still_parses() {
        use pg_worker_core::exit_class::ExitClass;
        use std::collections::BTreeSet;
        let p = ParkPacket {
            card: "c1".to_string(),
            tier: Tier::Free,
            exit_class: ExitClass::CheckFail,
            check_tail: "boom".to_string(),
        };

        let tagged = park_json_with_attempt(&p, Some(4), Some(vec!["running".into()]));
        let any = park_json_with_attempt(
            &p,
            Some(4),
            Some(
                crate::keeper::WORKER_LIVE_STATES
                    .iter()
                    .map(|s| (*s).to_owned())
                    .collect(),
            ),
        );
        assert_eq!(
            serde_json::from_str::<serde_json::Value>(&any).unwrap()["expected_state"],
            serde_json::json!(["open", "ready", "running", "waiting", "needs_input"])
        );
        assert_eq!(
            serde_json::from_str::<serde_json::Value>(&tagged).unwrap()["expected_state"],
            "running"
        );
        let v: serde_json::Value = serde_json::from_str(&tagged).unwrap();
        assert_eq!(v["attempt"], 4);
        assert_eq!(
            v.as_object()
                .unwrap()
                .keys()
                .map(String::as_str)
                .collect::<BTreeSet<_>>(),
            BTreeSet::from([
                "card",
                "tier",
                "exit_class",
                "check_tail",
                "attempt",
                "expected_state",
            ])
        );
        assert_eq!(ParkPacket::from_json(&tagged).unwrap(), p);
        // The activity reads the same key back out of the input.
        assert_eq!(park_attempt_of(&tagged), Some(4));

        let attempt_only = park_json_with_attempt(&p, Some(4), None);
        let v: serde_json::Value = serde_json::from_str(&attempt_only).unwrap();
        assert_eq!(
            v.as_object()
                .unwrap()
                .keys()
                .map(String::as_str)
                .collect::<BTreeSet<_>>(),
            BTreeSet::from(["card", "tier", "exit_class", "check_tail", "attempt"])
        );
        assert_eq!(v["attempt"], 4);
        assert_eq!(ParkPacket::from_json(&attempt_only).unwrap(), p);
        assert_eq!(park_attempt_of(&attempt_only), Some(4));

        // Unset attempt: the plain packet JSON, no extra key at all.
        let plain = park_json_with_attempt(&p, None, None);
        let v: serde_json::Value = serde_json::from_str(&plain).unwrap();
        assert_eq!(
            v.as_object()
                .unwrap()
                .keys()
                .map(String::as_str)
                .collect::<BTreeSet<_>>(),
            BTreeSet::from(["card", "tier", "exit_class", "check_tail"])
        );
        assert!(
            serde_json::from_str::<serde_json::Value>(&plain)
                .unwrap()
                .get("expected_state")
                .is_none()
        );
        assert_eq!(
            v,
            serde_json::from_str::<serde_json::Value>(&p.to_json()).unwrap()
        );
        assert_eq!(ParkPacket::from_json(&plain).unwrap(), p);
        assert_eq!(park_attempt_of(&plain), None);
    }
