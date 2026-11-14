//! Exercise the actual Python entry points from Rust regression tests.
use std::{path::PathBuf, process::Command};

fn root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..")
}
fn python(source: &str) {
    let output = Command::new(if cfg!(windows) { "python" } else { "python3" })
        .args(["-c", source])
        .current_dir(root())
        .output()
        .unwrap();
    assert!(output.status.success(), "{}{}", String::from_utf8_lossy(&output.stdout), String::from_utf8_lossy(&output.stderr));
}

#[test]
fn prepush_refuses_invalid_trailers_before_scrub_or_preflight() {
    python(r#"
import io, os, pathlib, runpy, subprocess, tempfile
from unittest.mock import patch
hook = runpy.run_path('tools/hooks/pre-push')
f = hook['push_main']
g = f.__globals__
with tempfile.TemporaryDirectory() as tmp:
    def git(*args):
        return subprocess.check_output(['git', '-C', tmp, *args], stderr=subprocess.PIPE).decode().strip()
    git('init', '-q')
    git('config', 'user.name', 'Test')
    git('config', 'user.email', 'test@example.invalid')
    git('commit', '--allow-empty', '-qm', 'baseline')
    base = git('rev-parse', 'HEAD')
    git('update-ref', 'refs/remotes/origin/main', base)
    path = pathlib.Path(tmp, '.github/workflows/tests.yml')
    path.parent.mkdir(parents=True)
    for trailer, expected in [('Hand-Edit: fixture', 'hand-edit touches auth/door'), ('Writer: codex/gpt-6.1-sol-medium', 'gate edit without Gate-Edit')]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(trailer)
        git('add', '.')
        git('commit', '-qm', 'fixture\n\nPlan-Item: plangraph:TEST\n' + trailer)
        head = git('rev-parse', 'HEAD')
        old = os.getcwd()
        os.chdir(tmp)
        try:
            err = io.StringIO()
            with patch.dict(g, file_size_gate=lambda: 0), patch.object(g['sc'], 'build_config', side_effect=AssertionError('scrub started before trailer refusal')), patch('sys.stdin', io.StringIO(f'refs/heads/topic {head} refs/heads/topic {base}\n')), patch('sys.stderr', err):
                assert f() != 0
            assert expected in err.getvalue(), err.getvalue()
        finally:
            os.chdir(old)
        git('reset', '--hard', base)
"#);
}

#[test]
fn browser_selection_and_failure_are_enforced() {
    python(r#"
import sys
sys.path.insert(0, 'tools')
import ci_select as ci
assert ci.select(['src/plangraph/keeper/store.py'])['browser'] is False
for path in ['src/plangraph/web/app.js', 'tests/browser/test_ui.py', 'requirements-ci.txt', '.github/workflows/tests.yml', 'vendor/htmx.js', 'tests/conftest.py', 'pyproject.toml']:
    assert ci.select([path])['browser'] is True, path
needs = {'select': {'result': 'success', 'outputs': {s: 'true' for s in ci.SUITES}}}
needs.update({s: {'result': 'success'} for s in ci.ALWAYS + ci.SUITES})
needs['browser']['result'] = 'failure'
assert 'browser: failure' in ci.gate(needs)
"#);
}

#[test]
fn full_tier_requires_windows_and_every_workflow_job() {
    python(r#"
import pathlib, sys
sys.path.insert(0, 'tools')
import ci_select as ci
needs = {'select': {'result': 'success', 'outputs': {'full': 'true', **{s: 'false' for s in ci.SUITES}}}}
needs.update({s: {'result': 'success'} for s in ci.ALWAYS + ci.SUITES})
needs['rust-windows'] = {'result': 'skipped'}
assert ci.gate(needs) == ['rust-windows: skipped'], ci.gate(needs)
workflow = pathlib.Path('.github/workflows/tests.yml').read_text()
import yaml
jobs = yaml.safe_load(workflow)['jobs']
assert set(jobs['ci-gate']['needs']) == set(jobs) - {'ci-gate'}
assert jobs['browser']['needs'] == 'select'
assert "needs.select.outputs.browser == 'true'" in jobs['browser']['if']
assert 'browser' in jobs['select']['outputs']
selection = jobs['select']['steps'][1]
assert 'PR_BASE_REF' in selection['env'] and 'PR_DRAFT' in selection['env']
assert 'PR_HEAD_REF' in selection['env']
assert 'batch/*' in selection['run'] and '--full' in selection['run']
for name in ('pytest', 'pytest-postgres'):
    for step in jobs[name]['steps']:
        if 'affected_tests.py' in step.get('run', ''):
            assert "needs.select.outputs.full" in step['run'], step
"#);
}

#[test]
fn land_requires_exact_full_run_and_records_reruns() {
    python(r#"
import sys
from unittest.mock import patch
sys.path.insert(0, 'tools')
import merge_batch_land as land
head = 'a' * 40
run = {'id': 7, 'head_sha': head, 'head_branch': 'batch/test', 'event': 'pull_request', 'status': 'completed', 'conclusion': 'success', 'run_attempt': 1}
names = ['select', 'scrub', 'file-size', 'new-code-lang', 'changelog-docs', 'pytest (0)', 'pytest (1)', 'pytest (2)', 'pytest-postgres (0)', 'pytest-postgres (1)', 'pytest-postgres (2)', 'browser', 'rust', 'rust-windows', 'rust-worker', 'rust-spike', 'ci-gate']
jobs = [{'name': n, 'status': 'completed', 'conclusion': 'success'} for n in names]
def api(*args):
    path = args[1]
    if '/jobs?' in path:
        return {'total_count': len(jobs), 'jobs': jobs}
    if '/attempts/' in path:
        return run
    if '/runs/' in path:
        return run
    return {'workflow_runs': [run]}
with patch.object(land, 'gh_json', side_effect=api):
    assert land.require_full_run('o/r', head, 'batch/test') == []
    for bad in ('skipped', 'failure', 'cancelled'):
        jobs[names.index('rust-windows')]['conclusion'] = bad
        try:
            land.require_full_run('o/r', head, 'batch/test')
            raise AssertionError('accepted ' + bad)
        except land.MergeBatchError as exc:
            assert 'rust-windows' in str(exc), str(exc)
    jobs[names.index('rust-windows')]['conclusion'] = 'success'
    jobs.pop(names.index('rust-windows'))
    try:
        land.require_full_run('o/r', head, 'batch/test')
        raise AssertionError('accepted missing windows')
    except land.MergeBatchError as exc:
        assert 'rust-windows' in str(exc)
    jobs.append({'name': 'rust-windows', 'status': 'completed', 'conclusion': 'success'})
    run['head_sha'] = 'b' * 40
    try:
        land.require_full_run('o/r', head, 'batch/test')
        raise AssertionError('accepted wrong head')
    except land.MergeBatchError:
        pass
    run['head_sha'] = head
    run['run_attempt'] = 2
    reruns = land.require_full_run('o/r', head, 'batch/test')
    assert reruns and 'rust-windows' in '\n'.join(reruns)
"#);
}

#[test]
fn red_main_freezes_land_except_a_head_bound_go_row() {
    python(r#"
import pathlib, sys, tempfile
from unittest.mock import patch
sys.path.insert(0, 'tools')
import merge_batch_land as land
head = 'a' * 40
run = {'head_sha': 'b' * 40, 'head_branch': 'main', 'status': 'completed', 'conclusion': 'failure'}
def api(*args):
    return {'object': {'sha': 'b' * 40}} if '/git/ref/' in args[1] else {'workflow_runs': [run]}
with patch.object(land, 'gh_json', side_effect=api), tempfile.TemporaryDirectory() as tmp:
    path = pathlib.Path(tmp, 'GO')
    for text in ['', 'GO ' + head, 'GO ' + 'c' * 40 + ' EXCEPTION:main-red-fix', 'GO ' + head + ' EXCEPTION:main-red-fix-extra']:
        path.write_text(text)
        try:
            land.require_main_green('o/r', head, str(path))
            raise AssertionError('red main accepted: ' + text)
        except land.MergeBatchError as exc:
            assert 'main_red' in str(exc), str(exc)
    path.write_text('GO ' + head + ' EXCEPTION:main-red-fix')
    land.require_main_green('o/r', head, str(path))
    run['conclusion'] = 'success'
    land.require_main_green('o/r', head, None)
    run['status'] = 'in_progress'
    try:
        land.require_main_green('o/r', head, None)
        raise AssertionError('pending main accepted')
    except land.MergeBatchError:
        pass
"#);
}
