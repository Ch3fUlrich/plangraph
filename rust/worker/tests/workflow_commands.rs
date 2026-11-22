//! Native workflow command tests: production workflow code, no service or env gate.
use pg_worker::{phase_workflow::Phase, workflow::Card};
use std::{cell::RefCell, rc::Rc};
use temporalio_common::{
    data_converters::DataConverter,
    protos::coresdk::{
        activity_result::{ActivityResolution, Success, activity_resolution},
        workflow_activation::{
            InitializeWorkflow, ResolveActivity, WorkflowActivation, WorkflowActivationJob,
            workflow_activation_job,
        },
        workflow_commands::{WorkflowCommand, workflow_command::Variant},
    },
};
use temporalio_workflow::{
    __private::sdk::{GuestWorkflowInstance, WorkflowHost, WorkflowInit, WorkflowInstance},
    BaseWorkflowContext,
};

#[derive(Default)]
struct Host(RefCell<Vec<WorkflowCommand>>);
impl WorkflowHost for Host {
    fn set_current_details(&self, _: String) {}
    fn push_command(&self, command: WorkflowCommand) {
        self.0.borrow_mut().push(command);
    }
}

fn base(host: Rc<Host>) -> BaseWorkflowContext {
    BaseWorkflowContext::from_raw(
        WorkflowInit {
            namespace: "default".into(),
            task_queue: "test".into(),
            run_id: "test".into(),
            initialize_workflow: InitializeWorkflow {
                workflow_id: "phase:test:card-key:1".into(),
                ..Default::default()
            },
        },
        DataConverter::default(),
        host,
        None,
        vec![],
    )
}

fn payload(
    value: serde_json::Value,
) -> temporalio_common::protos::temporal::api::common::v1::Payload {
    temporalio_common::protos::temporal::api::common::v1::Payload {
        metadata: std::collections::HashMap::from([("encoding".into(), b"json/plain".to_vec())]),
        data: serde_json::to_vec(&value).unwrap(),
        ..Default::default()
    }
}

fn resolve(instance: &mut dyn WorkflowInstance, seq: u32, value: serde_json::Value) {
    let waker = futures::task::noop_waker();
    instance
        .activate(
            WorkflowActivation {
                jobs: vec![WorkflowActivationJob {
                    variant: Some(workflow_activation_job::Variant::ResolveActivity(
                        ResolveActivity {
                            seq,
                            result: Some(ActivityResolution {
                                status: Some(activity_resolution::Status::Completed(Success {
                                    result: Some(payload(value)),
                                })),
                            }),
                            ..Default::default()
                        },
                    )),
                }],
                ..Default::default()
            },
            &waker,
        )
        .unwrap();
    instance.poll_routine(0, &waker).unwrap();
}

#[test]
fn child_upserts_card_key_not_phase_scoped_workflow_id() {
    let host = Rc::new(Host::default());
    let input = serde_json::from_value(serde_json::json!({
        "claim":{"card":"card-key","repo":"repo","base_sha":"abc","check_cmd":"true","allow":[],"forbid":[],"prompt":"fix"},
        "legs":[{"id":"one","tier":"free","model":"model"}],"workroot":"work","main_branch":"phase/test"
    })).unwrap();
    let mut instance =
        GuestWorkflowInstance::new_with_workflow(Card::default(), base(host.clone()), Some(input));
    instance
        .poll_routine(0, &futures::task::noop_waker())
        .unwrap();
    let commands = host.0.borrow();
    let memo = commands
        .iter()
        .find_map(|c| match &c.variant {
            Some(Variant::ModifyWorkflowProperties(p)) => p.upserted_memo.as_ref(),
            _ => None,
        })
        .expect("Card must emit a durable memo upsert");
    assert_eq!(
        serde_json::from_slice::<String>(&memo.fields["plan_key"].data).unwrap(),
        "card-key"
    );
}

#[test]
fn no_claim_emits_no_branch_activity_or_child() {
    let host = Rc::new(Host::default());
    let input = serde_json::from_value(serde_json::json!({
        "key":"test","repo":"repo","workroot":"work","base_branch":"main",
        "legs":[{"id":"one","tier":"free","model":"model"}]
    }))
    .unwrap();
    let mut instance =
        GuestWorkflowInstance::new_with_workflow(Phase::default(), base(host.clone()), Some(input));
    instance
        .poll_routine(0, &futures::task::noop_waker())
        .unwrap();
    let fixture: serde_json::Value =
        serde_json::from_str(include_str!("fixtures/phase_two_cards.json")).unwrap();
    let encoded = &fixture["events"][6]["attributes"]["ActivityTaskCompletedEventAttributes"]["result"]
        ["payloads"][0];
    // Decode through the SDK rather than maintaining another plan schema.
    let p: temporalio_common::protos::temporal::api::common::v1::Payload =
        serde_json::from_value(encoded.clone()).unwrap();
    let mut plan: serde_json::Value = serde_json::from_slice(&p.data).unwrap();
    plan["phase"] = "test".into();
    resolve(&mut instance, 1, plan.clone());
    for seq in 2..=3 {
        resolve(&mut instance, seq, serde_json::Value::Null);
    }
    resolve(&mut instance, 4, plan);
    let names: Vec<_> = host
        .0
        .borrow()
        .iter()
        .filter_map(|c| match &c.variant {
            Some(Variant::ScheduleActivity(a)) => Some(a.activity_type.clone()),
            Some(Variant::StartChildWorkflowExecution(_)) => panic!("no claim must start no child"),
            _ => None,
        })
        .collect();
    assert_eq!(
        names,
        [
            "CardActivities::phase_plan",
            "CardActivities::claim_card",
            "CardActivities::claim_card",
            "CardActivities::phase_plan"
        ]
    );
}

#[test]
fn failed_lazy_ensure_is_bounded_and_preserves_blocked_reason() {
    let host = Rc::new(Host::default());
    let input = serde_json::from_value(serde_json::json!({
        "key":"test","repo":"repo","workroot":"work","base_branch":"main",
        "legs":[{"id":"leg-1","tier":"free","model":"model"}]
    }))
    .unwrap();
    let mut instance =
        GuestWorkflowInstance::new_with_workflow(Phase::default(), base(host.clone()), Some(input));
    let waker = futures::task::noop_waker();
    instance.poll_routine(0, &waker).unwrap();
    let fixture: serde_json::Value =
        serde_json::from_str(include_str!("fixtures/phase_two_cards.json")).unwrap();
    let decode = |event: usize| {
        let p: temporalio_common::protos::temporal::api::common::v1::Payload = serde_json::from_value(
            fixture["events"][event]["attributes"]["ActivityTaskCompletedEventAttributes"]["result"]["payloads"][0].clone()).unwrap();
        serde_json::from_slice::<serde_json::Value>(&p.data).unwrap()
    };
    let mut plan = decode(6);
    plan["phase"] = "test".into();
    // The keeper still considers these cards startable when branch IO fails.
    resolve(&mut instance, 1, plan.clone());
    resolve(&mut instance, 2, decode(18));
    instance.activate(WorkflowActivation { jobs: vec![WorkflowActivationJob {
        variant: Some(workflow_activation_job::Variant::ResolveActivity(ResolveActivity {
            seq: 3, result: Some(ActivityResolution { status: Some(activity_resolution::Status::Failed(
                temporalio_common::protos::coresdk::activity_result::Failure {
                    failure: Some(temporalio_common::protos::temporal::api::failure::v1::Failure {
                        message: "phase branch IO failed".into(), ..Default::default()
                    }),
                    ..Default::default()
                }
            )) }), ..Default::default()
        })),
    }], ..Default::default() }, &waker).unwrap();
    use temporalio_workflow::__private::sdk::{
        MainRoutineCompletion, RoutineCompletion, TerminalOutcome,
    };
    let result = instance.poll_routine(0, &waker).unwrap();
    let Some(RoutineCompletion::Main(MainRoutineCompletion::Terminal(outcome))) = result.completion
    else {
        panic!("branch failure must terminate the phase: {result:?}");
    };
    let TerminalOutcome::Completed(output) = *outcome else {
        panic!("expected phase output")
    };
    let output: serde_json::Value = serde_json::from_slice(&output.data).unwrap();
    assert_eq!(output["waves"], 1);
    assert!(
        output["blocked"][0]
            .as_str()
            .unwrap()
            .contains("ensure_phase_branch failed")
    );
    let expected: Vec<_> = plan["cards"]
        .as_array()
        .unwrap()
        .iter()
        .map(|card| card["key"].clone())
        .collect();
    assert_eq!(output["not_merged"].as_array().unwrap(), &expected);
    // An infrastructure failure ends this wave; do not plan/claim another wave.
    let names: Vec<_> = host
        .0
        .borrow()
        .iter()
        .filter_map(|c| match &c.variant {
            Some(Variant::ScheduleActivity(a)) => Some(a.activity_type.clone()),
            _ => None,
        })
        .collect();
    assert_eq!(
        names,
        [
            "CardActivities::phase_plan",
            "CardActivities::claim_card",
            "CardActivities::ensure_phase_branch"
        ]
    );
}

fn card_instance(host: &Rc<Host>) -> GuestWorkflowInstance<Card> {
    let input = serde_json::from_value(serde_json::json!({
        "claim":{"card":"card-key","repo":"repo","base_sha":"abc","check_cmd":"true","allow":[],"forbid":[],"prompt":"fix"},
        "legs":[{"id":"one","tier":"free","model":"model"}],"workroot":"work","main_branch":"main"
    }))
    .unwrap();
    let mut instance =
        GuestWorkflowInstance::new_with_workflow(Card::default(), base(host.clone()), Some(input));
    instance
        .poll_routine(0, &futures::task::noop_waker())
        .unwrap();
    // Seq 1 is the tick; seq 2 is the first attempt activity.
    resolve(
        &mut instance,
        1,
        serde_json::json!({"unix_ms": 1_700_000_000_000u64, "jitter": 0.5}),
    );
    instance
}

fn fail(
    instance: &mut dyn WorkflowInstance,
    seq: u32,
    failure: temporalio_common::protos::temporal::api::failure::v1::Failure,
) -> Option<temporalio_workflow::__private::sdk::RoutineCompletion> {
    let waker = futures::task::noop_waker();
    instance
        .activate(
            WorkflowActivation {
                jobs: vec![WorkflowActivationJob {
                    variant: Some(workflow_activation_job::Variant::ResolveActivity(
                        ResolveActivity {
                            seq,
                            result: Some(ActivityResolution {
                                status: Some(activity_resolution::Status::Failed(
                                    temporalio_common::protos::coresdk::activity_result::Failure {
                                        failure: Some(failure),
                                        ..Default::default()
                                    },
                                )),
                            }),
                            ..Default::default()
                        },
                    )),
                }],
                ..Default::default()
            },
            &waker,
        )
        .unwrap();
    instance.poll_routine(0, &waker).unwrap().completion
}

/// `(activity type, decoded first argument)` of every activity scheduled so far.
fn scheduled(host: &Host) -> Vec<(String, serde_json::Value)> {
    host.0
        .borrow()
        .iter()
        .filter_map(|c| match &c.variant {
            Some(Variant::ScheduleActivity(a)) => Some((
                a.activity_type.clone(),
                a.arguments
                    .first()
                    .and_then(|p| serde_json::from_slice(&p.data).ok())
                    .unwrap_or(serde_json::Value::Null),
            )),
            _ => None,
        })
        .collect()
}

fn app_failure(kind: &str) -> temporalio_common::protos::temporal::api::failure::v1::Failure {
    use temporalio_common::protos::temporal::api::failure::v1::{
        ApplicationFailureInfo, Failure, failure::FailureInfo,
    };
    Failure {
        message: "Activity task failed".into(),
        cause: Some(Box::new(Failure {
            message: format!("{kind}: card-key"),
            failure_info: Some(FailureInfo::ApplicationFailureInfo(
                ApplicationFailureInfo {
                    r#type: kind.into(),
                    non_retryable: true,
                    ..Default::default()
                },
            )),
            ..Default::default()
        })),
        ..Default::default()
    }
}

#[test]
fn a_typed_admission_refusal_ends_the_card_before_any_report_or_probe() {
    use temporalio_workflow::__private::sdk::{
        MainRoutineCompletion, RoutineCompletion, TerminalOutcome,
    };
    for kind in [
        "card_not_allowlisted",
        "card_not_claimable",
        "card_attempt_overlap",
    ] {
        let host = Rc::new(Host::default());
        let mut instance = card_instance(&host);
        let done = fail(&mut instance, 2, app_failure(kind));
        let Some(RoutineCompletion::Main(MainRoutineCompletion::Terminal(outcome))) = done else {
            panic!("{kind}: the card must end on a typed refusal");
        };
        assert!(matches!(*outcome, TerminalOutcome::Failed(_)), "{kind}");
        let names: Vec<_> = scheduled(&host).into_iter().map(|(n, _)| n).collect();
        assert_eq!(
            names,
            ["CardActivities::tick", "attempt"],
            "{kind}: {names:?}"
        );
    }
}

#[test]
fn a_worker_crash_still_reports_and_probes_unlike_a_refusal() {
    // Control for the refusal test: an untyped failure takes the crash path, which
    // reports the attempt, so the refusal test's "no report" is not a harness artefact.
    let host = Rc::new(Host::default());
    let mut instance = card_instance(&host);
    let _ = fail(&mut instance, 2, app_failure("something_else"));
    let names: Vec<_> = scheduled(&host).into_iter().map(|(n, _)| n).collect();
    assert!(
        names.contains(&"CardActivities::report".to_string()),
        "{names:?}"
    );
}

#[test]
fn report_and_park_inputs_carry_the_check_tail_not_the_harness_output() {
    use pg_worker::attempt::AttemptResult;
    use pg_worker_core::exit_class::ExitClass;
    let host = Rc::new(Host::default());
    let mut instance = card_instance(&host);
    let result = AttemptResult {
        exit_class: ExitClass::ContractDefect,
        diff_hash: String::new(),
        check_hash: String::new(),
        check_tail: "red-first: CHECK-TAIL-MARKER".into(),
        context_overflow: false,
        branch: "attempt/card-key/1".into(),
        head: String::new(),
        output_tail: "HARNESS-OUTPUT-MARKER".into(),
        harness: None,
        model: None,
        combo: None,
        class: None,
        started_at: None,
        ended_at: None,
        tokens_in: None,
        tokens_out: None,
        cost: None,
        log_ref: None,
        question: None,
    };
    resolve(&mut instance, 2, serde_json::to_value(&result).unwrap());
    // Settle every activity the workflow schedules after the attempt until it parks.
    let mut settled = vec![1, 2];
    while !scheduled(&host)
        .iter()
        .any(|(n, _)| n == "CardActivities::park")
    {
        let (next, name) = host
            .0
            .borrow()
            .iter()
            .filter_map(|c| match &c.variant {
                Some(Variant::ScheduleActivity(a)) if !settled.contains(&a.seq) => {
                    Some((a.seq, a.activity_type.clone()))
                }
                _ => None,
            })
            .min()
            .unwrap_or_else(|| panic!("the workflow must reach its park: {:?}", scheduled(&host)));
        settled.push(next);
        // The janitor answers a count; every other activity here answers nothing.
        let answer = if name.ends_with("janitor") {
            0.into()
        } else {
            serde_json::Value::Null
        };
        resolve(&mut instance, next, answer);
    }
    let all = scheduled(&host);
    let find = |name: &str| {
        all.iter()
            .find(|(n, _)| n == name)
            .unwrap_or_else(|| panic!("{name} not scheduled: {all:?}"))
            .1
            .to_string()
    };
    let (report, park) = (find("CardActivities::report"), find("CardActivities::park"));
    for (what, body) in [("report", &report), ("park", &park)] {
        assert!(body.contains("CHECK-TAIL-MARKER"), "{what}: {body}");
        assert!(!body.contains("HARNESS-OUTPUT-MARKER"), "{what}: {body}");
    }
}
