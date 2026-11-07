//! Production Card resume behavior against a real Temporal server.
#![cfg(unix)]
mod support;
use pg_worker::{
    attempt::LegSpec,
    rollover::Resume,
    workflow::{AnswerSignal, Card, CardInput},
};
use pg_worker_core::{budget::ContextClass, config::Tier};
use std::time::Duration;
use support::*;
use temporalio_client::{WorkflowGetResultOptions, WorkflowStartOptions};

#[tokio::test]
async fn restored_card_preserves_attempt_answers_and_wait_baseline() {
    in_local(async {
        let addr = std::env::var("PG_TEMPORAL_ADDR").expect("real Temporal address required");
        let env = Env::new(&addr, "resume", r#"case "$PG_PROMPT" in *"prior-answer"*"queued-answer"*) echo ok > out.txt;; *"prior-answer"*) echo 'PG_QUESTION: {"text":"which db?"}';; *) exit 1;; esac"#, false).await;
        let claim = env.claim("resume", &["out.txt"], "test -f out.txt");
        let leg = LegSpec { id: "restored-leg".into(), tier: Tier::Free, model: "some/model".into(), harness: "opencode".into() };
        let input = CardInput {
            legacy: pg_worker::activities::CardInput { claim: claim.clone(), legs: vec![leg.clone()], workroot: env.workroot.display().to_string(), main_branch: "main".into(), question_wait_cap_s: Some(1), max_question_rounds: Some(3) },
            risk_tagged: false, heldout_cmd: String::new(), smoke_checks: vec![], exports: vec![], base_sha: None,
            resume: Some(Resume { history: vec![], base_sha: claim.base_sha, n: 7, waits: 100, waited: Duration::ZERO, context: ContextClass::K128, limited: vec![], leg, answers: vec!["prior-answer".into()], question_rounds: 0, signals: vec![AnswerSignal { question_id: 1, key: "resume".into(), text: "queued-answer".into(), source: "answer".into() }] }),
        };
        let id = format!("card:resume-{}", unique());
        let original = env.client.start_workflow(Card::run, input, WorkflowStartOptions::new(&env.task_queue, &id).build()).await.unwrap();
        let handle = env.client.get_workflow_handle::<Card>(&id);
        // Persist enough real signal events to cross the production history cap
        // before the first worker task. Unrelated answers cannot satisfy q1.
        for _ in 0..(pg_worker::rollover::EVENT_CAP + 10) {
            handle.signal(Card::answer, serde_json::json!({"question_id":99999,"key":"resume","text":"noise","source":"answer"}), Default::default()).await.unwrap();
        }
        // Start with the resume payload persisted, then poll using a fresh worker.
        env.resume();
        let out = tokio::time::timeout(Duration::from_secs(60), handle.get_result(WorkflowGetResultOptions::default())).await.unwrap().unwrap();
        assert_eq!(out.outcome, "done", "{out:?}");
        let ns: Vec<_> = env.keeper.attempts().iter().map(|a| a.n).collect();
        assert_eq!(ns, vec![8, 9]);
        assert!(env.keeper.question_timeouts().is_empty());
        let history = original.fetch_history(Default::default()).to_json().await.unwrap();
        let history = String::from_utf8(history).unwrap();
        assert!(history.contains("CONTINUED_AS_NEW") || history.contains("ContinuedAsNew"), "history-length gate must roll over before attempting work");
        env.stop();
    }).await;
}
