#![cfg(unix)]
#[path = "common/binary.rs"]
mod binary;
mod common;
#[allow(dead_code)]
#[path = "common/placement.rs"]
mod placement;
use placement::{payload, Fixture};
use serde_json::json;

#[test]
fn review_quota_falls_through_without_waiting_and_names_served_model() {
    for (no_fallthrough, ending) in [
        (false, "sleep 120"),
        (true, "sleep 120"),
        (false, "exit 0"),
        (true, "exit 0"),
    ] {
        let mut f = Fixture::new("exit 1", json!("review"), &[]);
        let first = json!({"id":"agy-hydrogen","harness":"agy","model":"gemini-4-hydrogen","family":"google"});
        let next = json!({"id":"sonnet","harness":"claude","model":"claude-sonnet-5-5","family":"anthropic"});
        let plan = json!({"first_leg":first,"legs":[first,{"id":"writer","harness":"codex","model":"gpt-6.1-sol","family":"gpt"},{"id":"unavailable","harness":"missing","model":"unavailable","family":"deepseek"},next],"skipped":[]});
        std::fs::write(
            f.state.path().join("route"),
            format!("#!/bin/sh\nprintf '%s' '{plan}'\n"),
        )
        .unwrap();
        let quota = "RESOURCE_EXHAUSTED (code 429): Individual quota reached";
        let bad = json!({"argv":["/bin/sh","-c",format!("printf '%s' '{quota}' >&2; {ending}")],"cwd":f.state.path(),"env":{},"prompt":"review","stdin":"review","warnings":null,"events":null});
        let good = json!({"argv":["/bin/sh","-c","printf '%s\\n' '{\"type\":\"system\",\"subtype\":\"init\",\"model\":\"claude-sonnet-5-5\"}' '{\"type\":\"result\",\"is_error\":false,\"result\":\"VERDICT: PASS\"}'"],"cwd":f.state.path(),"env":{},"prompt":"review","stdin":"review","warnings":null,"events":null});
        std::fs::write(f.state.path().join("launch"), format!("#!/bin/sh\nrequest=$(cat)\ncase \"$request\" in *gemini-4-hydrogen*) printf '%s' '{bad}';; *unavailable*) exit 1;; *) printf '%s' '{good}';; esac\n").replace(&format!("'{bad}'"), &shell_quote(&bad.to_string())).replace(&format!("'{good}'"), &shell_quote(&good.to_string()))).unwrap();
        let mut r = payload(f.call("spawn", json!({"prompt":"review","card":{"x":1},"role":"review","writer_families":["openai"],"wait_seconds":0,"no_fallthrough":no_fallthrough})));
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(30);
        while !pg_worker_core::run::record::Status::parse(r["status"].as_str().unwrap())
            .unwrap()
            .is_terminal()
        {
            assert!(std::time::Instant::now() < deadline, "{r}");
            std::thread::yield_now();
            r = payload(f.call("status", json!({"run_id":r["run_id"]})));
        }
        assert_eq!(
            r["status"],
            if no_fallthrough {
                "failed"
            } else {
                "completed"
            },
            "{r}"
        );
        assert!(r["attempts"][0]["skip_reason"]
            .as_str()
            .unwrap()
            .contains(quota));
        assert_eq!(
            r["attempts"].as_array().unwrap().len(),
            if no_fallthrough { 1 } else { 3 }
        );
        assert_eq!(
            std::fs::read_dir(f.state.path().join("runs"))
                .unwrap()
                .count(),
            1
        );
        if !no_fallthrough {
            assert_eq!(r["chosen_leg"], next);
            assert_eq!(r["served_model"], "claude-sonnet-5-5");
            assert_eq!(r["served_family"], "anthropic");
            assert_eq!(r["attempts"][1]["status"], "skipped");
            assert!(r["attempts"][1]["skip_reason"]
                .as_str()
                .is_some_and(|s| !s.is_empty()));
            assert_eq!(r["attempts"][2]["served_model"], r["served_model"]);
            assert!(r["reason"].is_null());
            let seat = std::path::Path::new(r["seat_dir"].as_str().unwrap());
            assert_eq!(
                std::fs::read_to_string(seat.join("review.txt")).unwrap(),
                "VERDICT: PASS"
            );
            let run = seat.parent().unwrap();
            assert!(std::fs::read_to_string(run.join("live-attempt-1.log"))
                .unwrap()
                .contains(quota));
        }
    }
}
fn shell_quote(value: &str) -> String {
    format!("'{}'", value.replace('\'', "'\\''"))
}

#[test]
fn python_review_quota_on_clean_exit_is_still_a_refusal() {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let out = std::process::Command::new("python3")
        .current_dir(&root).env("PYTHONPATH", root.join("src"))
        .args(["-c", r#"
from types import SimpleNamespace as NS
from plangraph.spawner.exit_outcome import fallthrough_reason
from plangraph.spawner.env_redact import early_refusal
from plangraph.routing_support.leg_state import LegStateStore
from pathlib import Path
from tempfile import TemporaryDirectory
quota = 'RESOURCE_EXHAUSTED (code 429): Individual quota reached'
with TemporaryDirectory() as tmp:
    service = NS(leg_reader=LegStateStore(Path(tmp) / 'legs.sqlite'))
    job = NS(stdout='', stderr=quota, exit_code=0, status='completed', events=[])
    record = dict(role='review')
    reason = fallthrough_reason(service, NS(model='gemini-4-hydrogen', harness='agy'), job, record)
    assert reason and quota in reason, (reason, record)
    assert early_refusal(quota) == quota
    import asyncio
    from plangraph.spawner.run_refusal import RefusalGate
    event = asyncio.Event()
    gate = RefusalGate(event, NS(role='review', start_time=0), lambda text: text)
    gate.candidate(quota, 120)
    assert event.is_set() and gate.job.refusal == quota

# The same-run fallthrough replay needs pytest fixtures: tests/test_spawner_seat_fallthrough.py::test_review_quota_on_clean_exit_falls_through_in_the_same_run.
"#]).output().unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
}

#[test]
fn keeper_counts_the_substitute_as_one_review() {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let out = std::process::Command::new("python3")
        .current_dir(&root).env("PYTHONPATH", root.join("src"))
        .args(["-c", r#"
from plangraph.keeper.evidence_spawner import SpawnerRecordAdapter
from pathlib import Path
from tempfile import TemporaryDirectory
from copy import deepcopy
head = 'a' * 40
with TemporaryDirectory() as tmp:
    directory = Path(tmp)
    (directory / 'host' / 'run-quota').mkdir(parents=True)
    adapter = SpawnerRecordAdapter(directory)
    final = dict(status='completed', served_model='claude-sonnet-5-5', served_family='anthropic',
                 stdout_tail='TEST_GAMING: no\nVERDICT: PASS\n')
    record = dict(run_id='run-quota', host='host', item_key='project:item', role='review',
                  served_model=final['served_model'], served_family='anthropic', status='completed', exit_code=0,
                  completion=dict(head_sha=head, finish_reason='end_turn'), stdout_tail=final['stdout_tail'],
                  attempts=[dict(status='failed', stderr_tail='RESOURCE_EXHAUSTED (code 429): Individual quota reached'), final])
    adapter.record = lambda *_: record
    seats = adapter.review_seats(dict(key='project:item', run_ids='["run-quota"]'), head, '')
    assert len(seats) == 1 and seats[0]['model'] == final['served_model'], seats
    for quota in ['quota reached', 'spinner\rRESOURCE_EXHAUSTED (code 429): Individual quota reached', 'HTTP 429: slow down', 'Error: provider quota refusal',
                  '{"autoos_provider_response":{"status":429}}']:
        record['attempts'][0]['stderr_tail'] = quota
        assert len(adapter.review_seats(dict(key='project:item', run_ids='["run-quota"]'), head, '')) == 1
    for bad in [dict(status='completed'), dict(status='failed', stderr_tail='transport error'),
                dict(status='failed', stdout_tail='I tested HTTP 429 quota reached'),
                dict(status='failed', stderr_tail='resource_exhausted was handled; rate limit resets in 5m')]:
        record['attempts'][0] = bad
        try:
            adapter.review_seats(dict(key='project:item', run_ids='["run-quota"]'), head, '')
        except ValueError:
            pass
        else:
            raise AssertionError('non-quota prior attempt was counted as one review')
"#]).output().unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
}
