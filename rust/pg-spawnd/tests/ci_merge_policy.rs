//! Exercise the actual Python entry points from Rust regression tests.
#[cfg(test)]
#[path = "../../test_support/python.rs"]
mod test_python;

use std::path::PathBuf;

fn root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..")
}
fn python(source: &str) {
    let fixture = tempfile::tempdir().unwrap();
    let mut command = test_python::command();
    pg_worker_core::git_test_support::configure(&mut command, fixture.path());
    let output = command
        .args(["-c", source])
        .current_dir(root())
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
}

#[test]
fn prepush_refuses_invalid_trailers_before_scrub_or_preflight() {
    python(
        r#"
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
    for trailer, expected in [('Hand-Edit: fixture', 'hand-edit touches auth/door'), ('Writer: codex/gpt-6.2-sol-medium', 'gate edit without Gate-Edit')]:
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
"#,
    );
}

#[test]
fn browser_selection_and_failure_are_enforced() {
    python(
        r#"
import sys
sys.path.insert(0, 'tools')
import ci_select as ci
assert ci.select(['src/plangraph/keeper/store.py'])['browser'] is False
workflow_selection = ci.select(['.github/workflows/tests.yml'])
assert {job for job, selected in workflow_selection.items() if selected} == {'pytest', 'rust'}
for path in ['src/plangraph/web/app.js', 'tests/browser/test_ui.py', 'requirements-ci.txt', 'vendor/htmx.js', 'tests/conftest.py', 'pyproject.toml', 'src/plangraph/render.py']:
    assert ci.select([path])['browser'] is True, path
needs = {'select': {'result': 'success', 'outputs': {s: 'true' for s in ci.SUITES}}}
needs.update({s: {'result': 'success'} for s in ci.ALWAYS + ci.SUITES})
needs['browser']['result'] = 'failure'
assert 'browser: failure' in ci.gate(needs)
"#,
    );
}

#[test]
fn full_tier_requires_windows_and_every_workflow_job() {
    python(
        r#"
import sys
sys.path.insert(0, 'tools')
import ci_select as ci
needs = {'select': {'result': 'success', 'outputs': {'full': 'true', **{s: 'false' for s in ci.SUITES}}}}
needs.update({s: {'result': 'success'} for s in ci.ALWAYS + ci.SUITES})
needs['rust-windows'] = {'result': 'skipped'}
assert ci.gate(needs) == ['rust-windows: skipped'], ci.gate(needs)
"#,
    );
    // A Windows checkout may carry CRLF line ends; the job scan below splits on LF.
    let workflow = std::fs::read_to_string(root().join(".github/workflows/tests.yml"))
        .unwrap()
        .replace("\r\n", "\n");
    let jobs = workflow.split_once("\njobs:\n").unwrap().1;
    let names: std::collections::BTreeSet<_> = jobs
        .lines()
        .filter_map(|line| {
            let name = line.strip_prefix("  ")?.strip_suffix(':')?;
            (!name.starts_with(' ') && !name.contains(' ')).then_some(name)
        })
        .collect();
    let gate = jobs.split_once("\n  ci-gate:\n").unwrap().1;
    let needed: std::collections::BTreeSet<_> = gate
        .lines()
        .find_map(|line| line.trim().strip_prefix("needs: ["))
        .unwrap()
        .trim_end_matches(']')
        .split(", ")
        .collect();
    assert_eq!(
        needed,
        names
            .into_iter()
            .filter(|name| *name != "ci-gate")
            .collect()
    );
    let browser = jobs
        .split_once("\n  browser:\n")
        .unwrap()
        .1
        .split("\n  pytest-postgres:")
        .next()
        .unwrap();
    assert!(browser.contains("needs: select"));
    assert!(browser.contains("if: needs.select.outputs.browser == 'true'"));
    let selection = jobs.split_once("\n  scrub:").unwrap().0;
    for input in [
        "PR_BASE_REF",
        "PR_HEAD_REF",
        "PR_LABELS",
        "--event",
        "--base-ref",
        "--head-ref",
        "--labels",
        "steps.sel.outputs.browser",
        "steps.sel.outputs.full",
    ] {
        assert!(selection.contains(input), "missing {input}");
    }
    assert!(!selection.contains("PR_DRAFT"));
    assert!(!selection.contains("--full"));
    for input in ["--event", "--base-ref", "--head-ref", "--labels"] {
        assert!(gate.contains(input), "ci-gate missing {input}");
    }
    let suite_jobs = jobs.split_once("\n  ci-gate:\n").unwrap().0;
    assert_eq!(suite_jobs.matches("needs.select.outputs.full").count(), 4);
    assert!(gate.contains("- name: Post ci-full evidence"));
    assert!(gate.contains(
        "success() && github.event_name == 'pull_request' && needs.select.outputs.full == 'true'"
    ));
    assert!(workflow.contains("types: [opened, synchronize, reopened, edited, labeled]"));
    assert!(!workflow.contains("ready_for_review"));
}

#[test]
fn land_requires_exact_full_run_and_records_reruns() {
    python(
        r#"
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
"#,
    );
}

#[test]
fn red_main_freezes_land_except_a_head_bound_go_row() {
    python(
        r#"
import pathlib, sys, tempfile
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0, 'tools')
import merge_batch_land as land
head = 'a' * 40
state = 'main_red'
def admit(argv, **kwargs):
    assert argv[:5] == ['fixture-plangraph', 'main-state', '--repo', 'o/r', '--admit'], argv
    assert argv[5:] in ([], ['--exception', 'EXCEPTION:main-red-fix']), argv
    allowed = state == 'green' or (state == 'main_red' and len(argv) == 7)
    return SimpleNamespace(returncode=0 if allowed else 3, stderr='' if allowed else state)

with (patch.dict(land.os.environ, PLANGRAPH_BIN='fixture-plangraph'),
      patch.object(land.subprocess, 'run', side_effect=admit), tempfile.TemporaryDirectory() as tmp):
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
    state = 'green'
    land.require_main_green('o/r', head, None)
    state = 'main_pending'
    try:
        land.require_main_green('o/r', head, None)
        raise AssertionError('pending main accepted')
    except land.MergeBatchError as exc:
        assert 'main_pending' in str(exc), str(exc)
"#,
    );
}

#[test]
fn rerun_audit_uploads_file_contents_to_a_comment() {
    python(
        r#"
import pathlib, sys
from unittest.mock import patch
sys.path.insert(0, 'tools')
import merge_batch_land as land
calls = []
def gh(*args):
    assert '-F' in args, 'body file must use --field; --raw-field sends a literal filename'
    payload = args[args.index('-F') + 1]
    assert payload.startswith('body=@')
    body = pathlib.Path(payload[len('body=@'):]).read_text()
    assert args[:4] == ('api', '--method', 'POST', 'repos/o/r/issues/7/comments')
    assert body.startswith('CI reruns')
    assert 'rust-windows' in body and 'attempt 2' in body
    calls.append(args)
with patch.object(land, 'gh_json', return_value=[]), patch.object(land, 'gh', side_effect=gh):
    land.record_reruns('o/r', 7, ['tests run 7 attempt 2: rust-windows'])
assert len(calls) == 1
"#,
    );
}

#[test]
fn prepush_only_skips_the_network_part_when_offline() {
    python(
        r#"
import runpy
from types import SimpleNamespace
from unittest.mock import patch
hook = runpy.run_path('tools/hooks/pre-push')
f = hook['commit_gate']
g = f.__globals__
clean = SimpleNamespace(returncode=0, stdout='', stderr='')
for response, expected in [(SimpleNamespace(returncode=1, stdout='', stderr='offline'), 0), (SimpleNamespace(returncode=0, stdout='invalid json', stderr=''), 1), (SimpleNamespace(returncode=0, stdout='[{"headRefOid": "bad"}]', stderr=''), 1)]:
    with patch.dict(g, git_out=lambda *a: 'b' * 40), patch.object(g['subprocess'], 'run', side_effect=[clean, response]):
        assert f('a' * 40) == expected
with patch.dict(g, git_out=lambda *a: 'b' * 40), patch.object(g['subprocess'], 'run', return_value=SimpleNamespace(returncode=1, stdout='bad trailers', stderr='')) as run:
    assert f('a' * 40) != 0
    assert run.call_count == 1
"#,
    );
}

#[test]
fn rerun_audit_does_not_trigger_an_edited_workflow_and_is_idempotent() {
    python(
        r#"
import pathlib, sys
from unittest.mock import patch
sys.path.insert(0, 'tools')
import merge_batch_land as land
comments, calls = [], []
def read(*args):
    assert args[0] == 'api' and '/issues/7/comments?' in args[1]
    return comments

def post(*args):
    assert args[:4] == ('api', '--method', 'POST', 'repos/o/r/issues/7/comments'), args
    assert '-F' in args
    payload = args[args.index('-F') + 1]
    body = pathlib.Path(payload[len('body=@'):]).read_text()
    assert 'run 7 attempt 2' in body and 'rust-windows' in body
    comments.append({'body': body})
    calls.append(args)
with patch.object(land, 'gh_json', side_effect=read), patch.object(land, 'gh', side_effect=post):
    land.record_reruns('o/r', 7, ['tests run 7 attempt 2: rust-windows'])
    land.record_reruns('o/r', 7, ['tests run 7 attempt 2: rust-windows'])
assert len(calls) == 1, calls
"#,
    );
}

/// Collect a Python input only when it lies inside the checkout. A path such as
/// `../../../tools/x.py` (an include_str! of a sibling file) is a real file only in a
/// worktree nested under another checkout, so it must not change the result by location.
const MAKE_ADD: &str = r####"
import os, pathlib
def make_add(root, inputs):
    def add(path):
        try: rel = pathlib.Path(os.path.normpath(path)).relative_to(os.path.normpath(root))
        except ValueError: return
        if path.is_file(): inputs.add(rel.as_posix())
    return add
"####;

#[test]
fn python_input_scan_ignores_paths_outside_the_checkout() {
    python(&format!(
        "{MAKE_ADD}{}",
        r####"
import tempfile
with tempfile.TemporaryDirectory() as tmp:
    base = pathlib.Path(tmp)
    (base / 'checkout/tools').mkdir(parents=True)
    (base / 'checkout/tools/in.py').write_text('')
    (base / 'tools').mkdir()
    (base / 'tools/out.py').write_text('')
    root = base / 'checkout'
    inputs = set()
    add = make_add(root, inputs)
    add(root / '../tools/out.py')
    add(root / 'tools/in.py')
    add(root / 'tools/../tools/in.py')
    add(root / 'tools/missing.py')
    assert inputs == {'tools/in.py'}, inputs
"####
    ));
}

#[test]
fn python_inputs_executed_by_spawnd_tests_select_rust_ci() {
    python(&format!(
        "{MAKE_ADD}{}",
        r####"
import ast, pathlib, re, sys
from fnmatch import fnmatchcase
sys.path.insert(0, 'tools')
import ci_select as ci
root = pathlib.Path.cwd()
inputs = set()
add = make_add(root, inputs)
def module(name):
    for base in (root / 'src', root / 'tools'):
        path = base.joinpath(*name.split('.'))
        add(path.with_suffix('.py'))
        add(path / '__init__.py')
# Rust embeds Python with raw strings; inspect their AST rather than matching
# just literal .join paths, which cannot see imports or runpy.run_path calls.
for source in (root / 'rust/pg-spawnd').rglob('*.rs'):
    text = source.read_text(encoding="utf-8")
    for match in re.finditer(r'r(#+)"(.*?)"\1', text, re.S):
        if 'ast.parse(match[2])' in match[2]: continue  # exclude this scanner's own program
        try: tree = ast.parse(match[2])
        except SyntaxError: continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names: module(alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                module(node.module)
                for alias in node.names: module(node.module + '.' + alias.name)
            elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                  and node.func.attr == 'run_path' and node.args
                  and isinstance(node.args[0], ast.Constant)):
                add(root / node.args[0].value)
    # Direct script arguments and the native writer's tools/<gate> loop.
    for path in re.findall(r'"([^"\n]+\.py)"', text):
        add(root / path.lstrip('/'))
        add(root / 'tools' / path)
required = {'src/plangraph/spawner/overflow.py', 'src/plangraph/keeper/evidence_spawner.py',
            'src/plangraph/routing_support/leg_state.py', 'tools/hooks/pre-push',
            'tools/merge_batch_land.py', 'tools/ci_select.py', 'tools/check_file_size.py',
            'tools/check_new_code_lang.py'}
assert required <= inputs, required - inputs
missing = sorted(p for p in inputs if not any(fnmatchcase(p, g) for g in ci.RUST_INPUTS))
assert not missing, 'Unlisted Rust Python inputs: ' + repr(missing)
assert all(ci.select([p])['rust'] for p in inputs)
"####
    ));
}
