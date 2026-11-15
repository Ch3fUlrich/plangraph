//! Exercise the legacy launch/outcome owners through their existing Python bridge.
use serde_json::Value;
use std::{path::Path, process::Command};

fn bridge(script: &str) -> Value {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let output = Command::new(if cfg!(windows) { "python" } else { "python3" })
        .current_dir(&root)
        .env("PYTHONPATH", root.join("src"))
        .args(["-c", script])
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).unwrap()
}

#[test]
fn claude_toolless_review_inlines_or_refuses_before_launch() {
    let rows = bridge(
        r#"
import asyncio, json, pathlib, subprocess, tempfile
from dataclasses import replace
from types import SimpleNamespace
from plangraph.planner.routing import DEFAULT_LEGS
from plangraph.spawner.seat_spawn import SeatSpawnMixin
from plangraph.spawner.inline_diff import InlineDiffError
from plangraph.spawner.exit_outcome import terminal_reason
from plangraph.spawner.service import SpawnService
leg = next(l for l in DEFAULT_LEGS if l.id == 'claude-haiku-5.5')
seat = SeatSpawnMixin()
rows = []
with tempfile.TemporaryDirectory() as tmp:
    def git(*args):
        return subprocess.check_output(['git', '-C', tmp, *args], text=True).strip()
    git('init', '-q')
    path = pathlib.Path(tmp) / 'change.txt'
    path.write_text('before\n')
    git('add', '.')
    git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test', 'commit', '-qm', 'base')
    base = git('rev-parse', 'HEAD')
    path.write_text('after-tool-less-review\n')
    git('add', '.')
    git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test', 'commit', '-qm', 'head')
    git('branch', '-M', 'main')
    git('remote', 'add', 'origin', tmp)
    ref = dict(repo=tmp, base=base, head=git('rev-parse', 'HEAD'))
    seat.runner = SimpleNamespace(default_cwd=tmp)
    for mode in ('prompt-only', 'tools'):
        for reference in (ref, None, dict(ref, head='missing-ref')):
            for explicit in (False, True):
                try:
                    prompt = seat._leg_prompt('Review', dict(diff_ref=reference), 'review',
                        replace(leg, tool_mode=mode), document_review=explicit)
                    rows.append(dict(mode=mode, valid=reference is ref, prompt=prompt))
                except InlineDiffError as exc:
                    record = dict(role='review', status='failed', exit_code=1,
                                  error='inline_diff_failed: ' + str(exc))
                    terminal_reason(record)
                    rows.append(dict(mode=mode, valid=reference is ref, error=str(exc),
                                     reason=record['reason']))
    service = SpawnService(state=pathlib.Path(tmp) / 'state',
        leg_reader=lambda *a, **k: {'state': 'available', 'evidence': None})
    service.runner.default_cwd = tmp
    for reference in (ref, None):
        card = dict(estimate_tokens=100, **({'diff_ref': reference} if reference else {}))
        record = asyncio.run(service.spawn(card,
            'Review', role='review', leg=leg.id, writer_families=['openai'],
            override_reason='hermetic regression', dry_run=True, wait_seconds=None))
        if reference is ref:
            job = service.runner.jobs[record['job_id']]
            rows.append(dict(mode='prompt-only', valid=True, prompt=job.task_prompt,
                             tools=job.command[job.command.index('--tools') + 1]))
        else:
            rows.append(dict(mode='prompt-only', valid=False, error=record['reason'] + ':',
                             reason=record['reason'], launched=bool(record.get('job_id'))))
print(json.dumps(rows))
"#,
    );
    for row in rows.as_array().unwrap() {
        if let Some(tools) = row.get("tools") {
            assert_eq!(tools, "");
        }
        if let Some(launched) = row.get("launched") {
            assert_eq!(launched, false);
        }
        if row["mode"] == "tools" {
            assert_eq!(row["prompt"], "Review");
        } else if row["valid"] == true {
            assert!(row["prompt"]
                .as_str()
                .unwrap()
                .contains("+after-tool-less-review"));
        } else {
            assert_eq!(row["reason"], "no_diff_for_toolless_seat");
            assert!(row["error"]
                .as_str()
                .unwrap()
                .starts_with("no_diff_for_toolless_seat:"));
        }
    }
}

#[test]
fn parameter_missing_review_is_a_named_harness_failure() {
    let rows = bridge(
        r#"
import json
from types import SimpleNamespace
from plangraph.spawner.exit_outcome import terminal_reason
rows = []
for with_job in (False, True):
    for status, code in (('completed', 0), ('failed', 1)):
        for text in ('<parameter_missing>', '<parameter_missing>\nVERDICT: ACCEPT'):
            record = dict(role='review', status=status, exit_code=code,
                attempts=[dict(stdout_tail=text)], stdout_tail='VERDICT: ACCEPT')
            terminal_reason(record, SimpleNamespace(stdout=text) if with_job else None)
            rows.append(dict(record, text=text))
for role, status, reason in (('implement', 'completed', None),
                            ('review', 'cancelled', 'cancelled'),
                            ('review', 'timed_out', 'timeout'),
                            ('review', 'failed', 'git_config_mutated')):
    record = dict(role=role, status=status, exit_code=0, reason=reason)
    before = record.copy()
    terminal_reason(record, SimpleNamespace(stdout='<parameter_missing>'))
    rows.append(dict(unchanged=record == before))
print(json.dumps(rows))
"#,
    );
    for row in rows.as_array().unwrap() {
        if row.get("unchanged").is_some() {
            assert_eq!(row["unchanged"], true);
        } else if row["text"] == "<parameter_missing>" {
            assert_eq!(row["status"], "failed");
            assert_eq!(row["reason"], "parameter_missing");
        } else {
            assert_ne!(row["reason"], "parameter_missing");
        }
    }
}
