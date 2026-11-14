//! Production Card resume behavior against a real Temporal server.
#![cfg(unix)]
#[path = "support/dev_server.rs"]
mod dev_server;
mod support;
use pg_worker::{
    attempt::LegSpec,
    rollover::Resume,
    workflow::{AnswerSignal, Card, CardInput},
};
use pg_worker_core::{budget::Attempt, exit_class::ExitClass};
use pg_worker_core::{budget::ContextClass, config::Tier};
use std::time::Duration;
use support::*;
use temporalio_client::{WorkflowGetResultOptions, WorkflowStartOptions};

#[test]
fn restored_card_preserves_attempt_answers_and_wait_baseline() {
    if std::env::var_os("PG_RESUME_TEST_CHILD").is_none() {
        let mut child = std::process::Command::new(std::env::current_exe().unwrap());
        child
            .args([
                "--exact",
                "restored_card_preserves_attempt_answers_and_wait_baseline",
                "--nocapture",
            ])
            .env("PG_RESUME_TEST_CHILD", "1")
            .env("PG_JAIL", "off")
            .env("PG_TEST_ALLOW_UNJAILED", "1");
        for (key, _) in std::env::vars_os() {
            let name = key.to_string_lossy();
            if matches!(
                name.as_ref(),
                "GIT_DIR" | "GIT_WORK_TREE" | "GIT_INDEX_FILE"
            ) || name.starts_with("GIT_CONFIG")
            {
                child.env_remove(key);
            }
        }
        let status = child.status().unwrap();
        assert!(status.success());
        return;
    }
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap()
        .block_on(run());
}

async fn run() {
    let Some(server) = dev_server::DevServer::start() else {
        return;
    };
    in_local(async {
        let addr = server.addr.clone();
        let env = Env::new(&addr, "resume", r#"case "$PG_PROMPT" in *"prior-answer"*"queued-answer"*) echo ok > out.txt;; *"prior-answer"*) echo 'PG_QUESTION: {"text":"which db?"}';; *) exit 1;; esac"#, false).await;
        let claim = env.claim("resume", &["out.txt"], "test -f out.txt");
        let leg = LegSpec { id: "restored-leg".into(), tier: Tier::Free, model: "some/model".into(), harness: "opencode".into() };
        let input = CardInput {
            legacy: pg_worker::activities::CardInput { claim: claim.clone(), legs: vec![leg.clone()], workroot: env.workroot.display().to_string(), main_branch: "main".into(), question_wait_cap_s: Some(1), max_question_rounds: Some(3) },
            risk_tagged: false, heldout_cmd: String::new(), smoke_checks: vec![], exports: vec![], base_sha: None,
            resume: Some(Resume { history: vec![Attempt { exit_class: ExitClass::UsageLimit, leg: "limited-leg".into(), tier: Tier::Free, diff_hash: "prior-diff".into(), check_hash: "prior-check".into(), context_overflow: false }], base_sha: claim.base_sha, n: 7, waits: 100, waited: Duration::from_secs(17), context: ContextClass::K128, limited: vec!["limited-leg".into()], leg, answers: vec!["prior-answer".into()], question_rounds: 0, signals: vec![AnswerSignal { question_id: 1, key: "resume".into(), text: "queued-answer".into(), source: "answer".into() }] }),
        };
        let expected = input.resume.clone().unwrap();
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
        assert_eq!(out.outcome, "done", "{out:?}; attempts={:?}; parked={:?}", env.keeper.attempts(), env.keeper.parked());
        let ns: Vec<_> = env.keeper.attempts().iter().map(|a| a.n).collect();
        assert_eq!(ns, vec![8, 9]);
        assert!(env.keeper.attempts().iter().all(|a| a.leg == "restored-leg"));
        assert!(env.keeper.question_timeouts().is_empty());
        let history = original.fetch_history(Default::default()).to_json().await.unwrap();
        let history = String::from_utf8(history).unwrap();
        assert!(history.contains("CONTINUED_AS_NEW") || history.contains("ContinuedAsNew"), "history-length gate must roll over before attempting work");
        let value: serde_json::Value = serde_json::from_str(&history).unwrap();
        let event = value["events"].as_array().unwrap().iter().find(|e| {
            e["attributes"].get("WorkflowExecutionContinuedAsNewEventAttributes").is_some()
        }).expect("Continue-As-New event");
        let data = event["attributes"]["WorkflowExecutionContinuedAsNewEventAttributes"]["input"]["payloads"][0]["data"].as_str().unwrap();
        let next: CardInput = serde_json::from_slice(&decode(data)).unwrap();
        let restored = next.resume.unwrap();
        assert_eq!(restored.history, expected.history);
        assert_eq!(restored.base_sha, expected.base_sha);
        assert_eq!(restored.n, expected.n);
        assert_eq!(restored.waits, expected.waits);
        assert_eq!(restored.waited, expected.waited);
        assert_eq!(restored.context, expected.context);
        assert_eq!(restored.limited, expected.limited);
        assert_eq!(restored.leg, expected.leg);
        assert_eq!(restored.answers, expected.answers);
        assert_eq!(restored.question_rounds, expected.question_rounds);
        assert_eq!(restored.signals.first(), expected.signals.first(), "old queued answer precedes new signals");
        assert!(restored.signals.iter().skip(1).all(|s| s.question_id == 99999));
        env.stop();
    }).await;
}

fn decode(s: &str) -> Vec<u8> {
    let (mut acc, mut bits) = (0u32, 0u32);
    let mut out = Vec::new();
    for c in s.bytes() {
        let v = match c {
            b'A'..=b'Z' => c - b'A',
            b'a'..=b'z' => c - b'a' + 26,
            b'0'..=b'9' => c - b'0' + 52,
            b'+' => 62,
            b'/' => 63,
            _ => continue,
        };
        acc = (acc << 6) | u32::from(v);
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            out.push((acc >> bits) as u8);
            acc &= (1 << bits) - 1;
        }
    }
    out
}
