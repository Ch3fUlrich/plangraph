#[cfg(test)]
#[path = "../../test_support/python.rs"]
mod test_python;

use serde_json::{json, Value};
use std::{
    io::Write,
    process::{Command, Stdio},
};

#[test]
fn train_report_pins_green_main_and_preserves_elapsed_time_across_overlaps() {
    let head = "a".repeat(40);
    let merge = "b".repeat(40);
    let row = |stage: &str, head: &str, attempt: u64, start: u64, end: u64| json!({"stage":stage,"head":head,"attempt":attempt,"started_at_ms":start,"ended_at_ms":end});
    let fixture = json!({
        "stages": [
            row("review", &head, 1, 10_000, 20_000),
            row("ci", &head, 7_001, 20_000, 200_000),
            row("delta_review", &head, 1, 30_000, 150_000),
            row("merge", &head, 1, 200_000, 210_000),
            row("ci", &merge, 8_001, 210_000, 270_000),
        ],
        "train": {"merge":merge},
        "accepts": [20_000],
        "jobs":[
            {"run_id":7,"attempt":1,"head":head,"name":"ci-gate","conclusion":"success","run_conclusion":"success"},
            {"run_id":8,"attempt":1,"head":merge,"name":"ci-gate","conclusion":"success","run_conclusion":"success"}
        ]
    });
    let result = report(&["train-1"], &fixture);
    assert_eq!(result["slowest"]["stage"], "ci");
    assert_eq!(result["slowest"]["minutes"], 3.0);
    assert_eq!(result["milestones"]["green_main_at_ms"], 270_000);
    assert_eq!(result["accept_to_green_ms"], 250_000);
}

#[test]
fn daily_report_names_the_slowest_stage_in_minutes() {
    let result = report(
        &["--day", "2026-01-01"],
        &json!({"stages": [
            {"stage":"review", "batch_key":"part-1", "started_at_ms":1_767_225_600_000_u64, "ended_at_ms":1_767_225_660_000_u64},
            {"stage":"push", "batch_key":"train-2", "started_at_ms":1_767_225_900_000_u64, "ended_at_ms":1_767_226_050_000_u64},
            {"stage":"merge", "batch_key":"train-2", "started_at_ms":1_767_226_050_000_u64, "ended_at_ms":1_767_226_080_000_u64},
        ]}),
    );
    assert_eq!(result["day"], "2026-01-01");
    assert_eq!(
        result["slowest"],
        json!({
            "stage":"push", "batch_key":"train-2", "duration_ms":150_000, "minutes":2.5,
        })
    );
    assert_eq!(result["summary"], "push 2.5 minutes");
}

fn report(args: &[&str], fixture: &Value) -> Value {
    let mut child = Command::new(env!("CARGO_BIN_EXE_plangraph"))
        .args(["landing", "report", "example/repo"])
        .args(args)
        .arg("--fixture")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    child
        .stdin
        .take()
        .unwrap()
        .write_all(fixture.to_string().as_bytes())
        .unwrap();
    let output = child.wait_with_output().unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).unwrap()
}

#[test]
#[cfg(unix)]
fn keeper_timing_actions_preserve_attempts_and_filter_by_utc_completion_day() {
    run_keeper_fixture(
        r#"
from plangraph.keeper.auth import Principal, add_principal
from plangraph.keeper.db import open_db
from plangraph.keeper.store import Store

db = open_db(':memory:', create=True, backend='sqlite')
add_principal(db, 'lander', 'router', [])
principal = Principal('lander', 'router', [])
store = Store(db)
repo = 'example/repo'
day = 86_400_000
head, part = 'a'*40, 'b'*40
batch = 'train-1'

def land(action, **fields):
    return store.land(principal, {'repo': repo, 'action': action, **fields})

lease = land('acquire', owner='test')
land('prepare', pr=17, base='c'*40, head=head, batch_key=batch,
     parts=[dict(pr=5, head=part, items=['example:PART'])], **lease)
land('stage', batch_key='example:PART', stage='review', head=part, attempt=1,
     started_at_ms=day-1000, ended_at_ms=day)
land('stage', batch_key=batch, stage='push', head=head, attempt=1,
     started_at_ms=day, ended_at_ms=day+1000)

def ci(attempt, start, end, job_end):
    return land('ci_timings', batch_key=batch, run_id=12, run_attempt=attempt,
                run_conclusion='success',
                stage=dict(stage='ci', head=head, attempt=12000+attempt,
                           started_at_ms=start, ended_at_ms=end),
                jobs=[dict(job_id=42, name='ci-gate', conclusion='success',
                           started_at_ms=start, ended_at_ms=job_end)])

ci(1, day+1000, day+6000, day+4000)
ci(1, day+1000, day+7000, day+5000)
ci(2, day+10_000, day+20_000, day+19_000)
land('stage', batch_key='other-train', stage='merge', head=head, attempt=1,
     started_at_ms=day, ended_at_ms=2*day)
land('stage', repo='other/repo', batch_key=batch, stage='merge', head=head, attempt=1,
     started_at_ms=day, ended_at_ms=day+1)

timeline = land('timeline', batch_key=batch)
assert timeline['train']['pr'] == 17
assert timeline['accepts'] == [day]
assert [(r['stage'], r['attempt'], r['ended_at_ms']) for r in timeline['stages']] == [
    ('review', 1, day), ('push', 1, day+1000),
    ('ci', 12001, day+6000), ('ci', 12002, day+20_000)]
assert [(j['run_id'], j['attempt'], j['job_id'], j['duration_ms']) for j in timeline['jobs']] == [
    (12, 1, 42, 3000), (12, 2, 42, 9000)]
assert land('stage_day', start=day)['stages'] == timeline['stages']
assert [r['batch_key'] for r in land('stage_day', start=2*day)['stages']] == ['other-train']
"#,
    );
}

#[test]
#[cfg(unix)]
fn go_request_includes_the_keeper_timeline_before_the_plan_block() {
    run_keeper_fixture(
        r#"
import io, json
from argparse import Namespace
from contextlib import redirect_stdout
from unittest.mock import Mock, patch
import merge_batch_build as build
import merge_batch_land as landing
import merge_batch_util as util

head = 'a'*40
state = dict(repo='example/repo', name='train-1', pr=17, worktree='fixture', status='pushed',
             base_head='b'*40, head=head, plan='example:TRAIN',
             merged=[dict(pr=5, head='c'*40, merge='d'*40, items=['example:PART'])],
             ci=dict(head=head, id=12, state='completed', conclusion='success'))
report = Mock(return_value=Namespace(stdout=json.dumps({'summary': 'ci=2.5m push=0.5m'})))
save = Mock()
output = io.StringIO()
with (patch.object(landing, 'load_state', return_value=state),
      patch.object(landing, 'git', side_effect=[Namespace(stdout=head), Namespace(stdout='1 file changed')]),
      patch.object(util, 'git', side_effect=[
          Namespace(stdout=''), Namespace(stdout='c'*40),
          Namespace(stdout='Part\n\nPlan-Item: example:PART\n')]),
      patch.object(landing, 'gh_json', return_value=dict(state='OPEN', headRefOid=head)),
      patch.object(landing, 'require_part_seats', return_value={5: ['run-fixture']}),
      patch.object(landing, 'batch_workflow_gate'),
      patch.object(landing, 'keeper_migration', return_value=23),
      patch.object(landing, 'keeper_landing_configured', return_value=True),
      patch.object(build, 'run', report),
      patch.object(landing, 'save_state', save),
      redirect_stdout(output)):
    assert landing.cmd_go_req(Namespace(name='train-1')) == 0

report.assert_called_once_with(['plangraph', 'landing', 'report', 'example/repo', 'example:TRAIN'], timeout=35)
save.assert_called_once_with(state)
assert state['go_requested_head'] == head
assert output.getvalue().splitlines() == [
    f'GO-REQ #17 example/repo head {head}; diff stat: 1 file changed; parts: '
    f'#5 sha={state["merged"][0]["head"]} review=run-fixture verdict=ACCEPT; '
    'CI run 12 state=completed conclusion=success; GO-KEYS [example:PART,example:TRAIN]; '
    'timeline: ci=2.5m push=0.5m; '
    'MIGRATION 23; PLAN example:TRAIN conforms=yes deviations=none']
"#,
    );
}

#[cfg(unix)]
fn run_keeper_fixture(fixture: &str) {
    let repo = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let output = test_python::command()
        .env("PYTHONPATH", repo.join("src"))
        .current_dir(repo.join("tools"))
        .args(["-c", fixture])
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
}
