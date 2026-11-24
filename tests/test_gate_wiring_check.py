from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = '.github/workflows/tests.yml'
WORKFLOW = '''on: [pull_request, push]
jobs:
  ci-gate:
    runs-on: ubuntu-latest
    steps:
      - name: Gate
        run: python3 tools/ci_select.py gate
      - name: Commit trailers
        run: python3 base.py check
  pytest:
    runs-on: ubuntu-latest
    steps:
      - run: python3 -m pytest
'''
WORKFLOW_NO_CI_GATE = '''on: [pull_request, push]
jobs:
  pytest:
    runs-on: ubuntu-latest
    steps:
      - run: python3 -m pytest
'''


def _load_tool():
    spec = importlib.util.spec_from_file_location('gate_wiring_check', REPO_ROOT / 'tools' / 'gate_wiring_check.py')
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


gate_wiring_check = _load_tool()
main = gate_wiring_check.main
wiring_at = gate_wiring_check.wiring_at
has_gate_edit = gate_wiring_check.has_gate_edit


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=repo, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / 'repo'
    path.mkdir()
    _run(path, 'git', 'init', '-q')
    _run(path, 'git', 'config', 'user.name', 'ci')
    email = 'ci' + '@' + 'example.invalid'
    _run(path, 'git', 'config', 'user.email', email)
    return path


def _commit(repo: Path, msg: str, files: dict[str, str] | None = None) -> str:
    if files:
        for name, content in files.items():
            target = repo / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
    _run(repo, 'git', 'add', '-A')
    _run(repo, 'git', 'commit', '-q', '--allow-empty', '-m', msg)
    result = _run(repo, 'git', 'rev-parse', 'HEAD')
    return result.stdout.strip()


def test_unchanged_wiring(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    base = _commit(repo, 'base', {WORKFLOW_PATH: WORKFLOW})
    head = _commit(repo, 'edit pytest only', {WORKFLOW_PATH: WORKFLOW.replace('python3 -m pytest', 'python3 -m pytest -q')})
    monkeypatch.chdir(repo)
    assert main(['--base', base, '--head', head]) == 0
    captured = capsys.readouterr()
    assert 'gate wiring unchanged' in captured.out


def test_gate_change_without_gate_edit(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    base = _commit(repo, 'base', {WORKFLOW_PATH: WORKFLOW})
    changed = WORKFLOW.replace('python3 tools/ci_select.py gate', 'python3 tools/ci_select.py gate || true')
    head = _commit(repo, 'change gate', {WORKFLOW_PATH: changed})
    assert has_gate_edit(base, head, cwd=repo) is False
    monkeypatch.chdir(repo)
    assert main(['--base', base, '--head', head]) == 1
    captured = capsys.readouterr()
    assert 'WITHOUT Gate-Edit' in captured.err
    assert '|| true' in captured.out


def test_gate_change_with_gate_edit(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    base = _commit(repo, 'base', {WORKFLOW_PATH: WORKFLOW})
    changed = WORKFLOW.replace('python3 tools/ci_select.py gate', 'python3 tools/ci_select.py gate || true')
    head = _commit(repo, 'change gate\n\nGate-Edit: D-2209\n', {WORKFLOW_PATH: changed})
    assert has_gate_edit(base, head, cwd=repo) is True
    monkeypatch.chdir(repo)
    assert main(['--base', base, '--head', head]) == 0
    captured = capsys.readouterr()
    assert 'WITH Gate-Edit' in captured.out


def test_gate_edit_leading_space_refused(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    base = _commit(repo, 'base', {WORKFLOW_PATH: WORKFLOW})
    changed = WORKFLOW.replace('python3 tools/ci_select.py gate', 'python3 tools/ci_select.py gate || true')
    head = _commit(repo, 'change gate\n\n Gate-Edit: D-2209\n', {WORKFLOW_PATH: changed})
    assert has_gate_edit(base, head, cwd=repo) is False
    monkeypatch.chdir(repo)
    assert main(['--base', base, '--head', head]) == 1
    captured = capsys.readouterr()
    assert 'WITHOUT Gate-Edit' in captured.err


def test_gate_edit_bad_id_refused(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    base = _commit(repo, 'base', {WORKFLOW_PATH: WORKFLOW})
    changed = WORKFLOW.replace('python3 tools/ci_select.py gate', 'python3 tools/ci_select.py gate || true')
    head = _commit(repo, 'change gate\n\nGate-Edit: D-x\n', {WORKFLOW_PATH: changed})
    assert has_gate_edit(base, head, cwd=repo) is False
    monkeypatch.chdir(repo)
    assert main(['--base', base, '--head', head]) == 1
    captured = capsys.readouterr()
    assert 'WITHOUT Gate-Edit' in captured.err


def test_missing_ci_gate_at_head_refused(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    base = _commit(repo, 'base', {WORKFLOW_PATH: WORKFLOW})
    head = _commit(repo, 'remove ci-gate\n\nGate-Edit: D-2209\n', {WORKFLOW_PATH: WORKFLOW_NO_CI_GATE})
    monkeypatch.chdir(repo)
    assert main(['--base', base, '--head', head]) == 1
    captured = capsys.readouterr()
    assert 'missing at head' in captured.err


def test_triggers_changed_without_gate_edit(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    base = _commit(repo, 'base', {WORKFLOW_PATH: WORKFLOW})
    changed = WORKFLOW.replace('on: [pull_request, push]', 'on: [push]')
    head = _commit(repo, 'drop pull_request', {WORKFLOW_PATH: changed})
    monkeypatch.chdir(repo)
    assert main(['--base', base, '--head', head]) == 1
    captured = capsys.readouterr()
    assert 'WITHOUT Gate-Edit' in captured.err


def test_continue_on_error_changed_without_gate_edit(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    base = _commit(repo, 'base', {WORKFLOW_PATH: WORKFLOW})
    changed = WORKFLOW.replace(
        '      - name: Gate\n        run: python3 tools/ci_select.py gate',
        '      - name: Gate\n        continue-on-error: true\n        run: python3 tools/ci_select.py gate',
    )
    head = _commit(repo, 'continue on error', {WORKFLOW_PATH: changed})
    monkeypatch.chdir(repo)
    assert main(['--base', base, '--head', head]) == 1
    captured = capsys.readouterr()
    assert 'WITHOUT Gate-Edit' in captured.err


def test_missing_ci_gate_at_base(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = _commit(repo, 'base without ci-gate', {WORKFLOW_PATH: WORKFLOW_NO_CI_GATE})
    head = _commit(repo, 'add ci-gate', {WORKFLOW_PATH: WORKFLOW})
    monkeypatch.chdir(repo)
    assert main(['--base', base, '--head', head]) == 2


def test_bad_revision(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    base = _commit(repo, 'base', {WORKFLOW_PATH: WORKFLOW})
    monkeypatch.chdir(repo)
    assert main(['--base', 'does-not-exist', '--head', base]) == 2
    captured = capsys.readouterr()
    assert captured.err


def test_real_repo_workflow_parses() -> None:
    wiring = wiring_at('HEAD', '.github/workflows/tests.yml', 'ci-gate', cwd=REPO_ROOT)
    assert isinstance(wiring, dict)
    assert 'steps' in wiring['job']


def test_gate_edit_must_be_on_every_workflow_commit(repo, monkeypatch, capsys):
    base = _commit(repo, 'base', {WORKFLOW_PATH: WORKFLOW})
    first = WORKFLOW.replace('python3 tools/ci_select.py gate', 'python3 tools/ci_select.py gate -v')
    _commit(repo, 'a\n\nGate-Edit: D-2209\n', {WORKFLOW_PATH: first})
    second = first.replace('-v', '|| true')
    head = _commit(repo, 'b', {WORKFLOW_PATH: second})
    monkeypatch.chdir(repo)
    assert main(['--base', base, '--head', head]) == 1
    captured = capsys.readouterr()
    assert 'WITHOUT Gate-Edit' in captured.err


def test_gate_edit_on_every_workflow_commit_passes(repo, monkeypatch, capsys):
    base = _commit(repo, 'base', {WORKFLOW_PATH: WORKFLOW})
    first = WORKFLOW.replace('python3 tools/ci_select.py gate', 'python3 tools/ci_select.py gate -v')
    _commit(repo, 'a\n\nGate-Edit: D-2209\n', {WORKFLOW_PATH: first})
    second = first.replace('-v', '|| true')
    head = _commit(repo, 'b\n\nGate-Edit: D-2209\n', {WORKFLOW_PATH: second})
    monkeypatch.chdir(repo)
    assert main(['--base', base, '--head', head]) == 0
    captured = capsys.readouterr()
    assert 'WITH Gate-Edit' in captured.out

def test_gate_edit_in_body_does_not_count(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    base = _commit(repo, 'base', {WORKFLOW_PATH: WORKFLOW})
    changed = WORKFLOW.replace('python3 tools/ci_select.py gate', 'python3 tools/ci_select.py gate || true')
    head = _commit(repo, 'x\n\nGate-Edit: D-2209\n\nmore prose after\n', {WORKFLOW_PATH: changed})
    monkeypatch.chdir(repo)
    assert main(['--base', base, '--head', head]) == 1
    captured = capsys.readouterr()
    assert 'WITHOUT Gate-Edit' in captured.err


def test_gate_edit_with_co_author_paragraph_counts(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    base = _commit(repo, 'base', {WORKFLOW_PATH: WORKFLOW})
    changed = WORKFLOW.replace('python3 tools/ci_select.py gate', 'python3 tools/ci_select.py gate || true')
    head = _commit(repo, 'x\n\nGate-Edit: D-2209\n\nCo-Authored-By: A B <a@example.invalid>\n', {WORKFLOW_PATH: changed})
    monkeypatch.chdir(repo)
    assert main(['--base', base, '--head', head]) == 0
    captured = capsys.readouterr()
    assert 'WITH Gate-Edit' in captured.out


def test_control_char_in_message_does_not_hide_commit(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    base = _commit(repo, 'base', {WORKFLOW_PATH: WORKFLOW})
    changed = WORKFLOW.replace('python3 tools/ci_select.py gate', 'python3 tools/ci_select.py gate || false')
    _commit(repo, 'a\x01b\n\nGate-Edit: D-2209\n', {WORKFLOW_PATH: changed})
    changed = WORKFLOW.replace('python3 tools/ci_select.py gate', 'python3 tools/ci_select.py gate || exit 1')
    head = _commit(repo, 'b', {WORKFLOW_PATH: changed})
    monkeypatch.chdir(repo)
    assert main(['--base', base, '--head', head]) == 1
    captured = capsys.readouterr()
    assert 'WITHOUT Gate-Edit' in captured.err


def test_gate_edit_missing_on_earlier_commit_refused(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    base = _commit(repo, 'base', {WORKFLOW_PATH: WORKFLOW})
    first = WORKFLOW.replace('python3 tools/ci_select.py gate', 'python3 tools/ci_select.py gate || true')
    first_sha = _commit(repo, 'a', {WORKFLOW_PATH: first})
    second = first.replace('python3 tools/ci_select.py gate || true', 'python3 tools/ci_select.py gate || true -v')
    second_sha = _commit(repo, 'b\n\nGate-Edit: D-2209\n', {WORKFLOW_PATH: second})
    monkeypatch.chdir(repo)
    assert main(['--base', base, '--head', second_sha]) == 1
    captured = capsys.readouterr()
    assert 'WITHOUT Gate-Edit' in captured.err
    assert first_sha in captured.err

def test_control_char_on_offending_commit_does_not_hide_it(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    base = _commit(repo, 'base', {WORKFLOW_PATH: WORKFLOW})
    first = WORKFLOW.replace('python3 tools/ci_select.py gate', 'python3 tools/ci_select.py gate || false')
    _commit(repo, 'a\n\nGate-Edit: D-2209\n', {WORKFLOW_PATH: first})
    second = first.replace('python3 tools/ci_select.py gate || false', 'python3 tools/ci_select.py gate || false || exit 1')
    second_sha = _commit(repo, 'b\x01c', {WORKFLOW_PATH: second})
    monkeypatch.chdir(repo)
    assert main(['--base', base, '--head', second_sha]) == 1
    captured = capsys.readouterr()
    assert 'WITHOUT Gate-Edit' in captured.err
    assert second_sha in captured.err

GO_STEP = '''      - name: Verify computed GO
        if: ${{ github.event_name == 'pull_request' }}
        run: |
          plangraph land-train go-verify --evidence "$GO_EVIDENCE"
'''
WORKFLOW_GO = WORKFLOW.replace('      - name: Commit trailers', GO_STEP + '      - name: Commit trailers')


def _go_job(text: str) -> dict:
    import yaml
    return yaml.safe_load(text)['jobs']['ci-gate']


def test_go_verify_step_found_and_intact() -> None:
    job = _go_job(WORKFLOW_GO)
    assert gate_wiring_check.go_verify_step(job) is not None
    assert gate_wiring_check.go_verify_problems(job, job) == []


@pytest.mark.parametrize('edit', [
    lambda t: t.replace('plangraph land-train go-verify --evidence "$GO_EVIDENCE"', 'echo skipped'),
    lambda t: t.replace('--evidence "$GO_EVIDENCE"', '--evidence "$GO_EVIDENCE" || true'),
    lambda t: t.replace('        if: ${{ github.event_name == \'pull_request\' }}\n', '        continue-on-error: true\n'),
    lambda t: t.replace("github.event_name == 'pull_request'", 'true'),
    lambda t: t.replace(GO_STEP, ''),
])
def test_go_verify_step_removed_or_loosened_is_a_problem(edit) -> None:
    base = _go_job(WORKFLOW_GO)
    head = _go_job(edit(WORKFLOW_GO))
    assert gate_wiring_check.go_verify_problems(base, head)


def test_go_verify_added_where_base_has_none_is_no_problem() -> None:
    assert gate_wiring_check.go_verify_problems(_go_job(WORKFLOW), _go_job(WORKFLOW_GO)) == []


def test_main_refuses_removal_of_go_verify_even_with_gate_edit(
        repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    base = _commit(repo, 'base', {WORKFLOW_PATH: WORKFLOW_GO})
    head = _commit(repo, 'drop it\n\nGate-Edit: D-2209\n', {WORKFLOW_PATH: WORKFLOW})
    monkeypatch.chdir(repo)
    assert main(['--base', base, '--head', head]) == 1
    assert 'go-verify' in capsys.readouterr().err


def test_main_accepts_adding_go_verify_with_gate_edit(
        repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    base = _commit(repo, 'base', {WORKFLOW_PATH: WORKFLOW})
    head = _commit(repo, 'add it\n\nGate-Edit: D-2209\n', {WORKFLOW_PATH: WORKFLOW_GO})
    monkeypatch.chdir(repo)
    assert main(['--base', base, '--head', head]) == 0


def _repo_go_step() -> dict:
    import yaml
    doc = yaml.safe_load((REPO_ROOT / WORKFLOW_PATH).read_text())
    return gate_wiring_check.go_verify_step(doc['jobs']['ci-gate'])


def test_the_landing_workflow_runs_go_verify_after_the_gate_and_blocks() -> None:
    import yaml
    doc = yaml.safe_load((REPO_ROOT / WORKFLOW_PATH).read_text())
    steps = doc['jobs']['ci-gate']['steps']
    step = _repo_go_step()
    assert step is not None
    assert steps.index(step) > next(i for i, s in enumerate(steps) if s.get('name') == 'Gate')
    assert 'continue-on-error' not in step
    assert 'land-train go-verify --evidence' in step['run']
    assert gate_wiring_check.go_verify_problems(doc['jobs']['ci-gate'], doc['jobs']['ci-gate']) == []


def _run_step(tmp_path: Path, evidence: Path | None, cargo_status: int) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    '''Run the workflow step's script with a stub cargo that records its arguments.'''
    import os
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    log = tmp_path / 'cargo.log'
    stub = bindir / 'cargo'
    stub.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{log}"\nexit {cargo_status}\n')
    stub.chmod(0o755)
    env = dict(os.environ, PATH=f'{bindir}:{os.environ["PATH"]}', RUNNER_TEMP=str(tmp_path / 'tmp'))
    env['GO_EVIDENCE'] = str(evidence) if evidence else str(tmp_path / 'absent.json')
    result = subprocess.run(['bash', '-c', _repo_go_step()['run']], cwd=REPO_ROOT, env=env,
                            capture_output=True, text=True)
    return result, log.read_text().splitlines() if log.exists() else []


def test_the_step_does_nothing_without_an_evidence_file(tmp_path: Path) -> None:
    result, calls = _run_step(tmp_path, None, 1)
    assert result.returncode == 0
    assert calls == []


def test_the_step_fails_when_the_verifier_refuses_the_evidence(tmp_path: Path) -> None:
    evidence = tmp_path / 'e.json'
    evidence.write_text('{}')
    result, calls = _run_step(tmp_path, evidence, 1)
    assert result.returncode != 0
    assert len(calls) == 1 and f'land-train go-verify --evidence {evidence}' in calls[0]


def test_the_step_passes_when_the_verifier_accepts(tmp_path: Path) -> None:
    evidence = tmp_path / 'e.json'
    evidence.write_text('{}')
    result, calls = _run_step(tmp_path, evidence, 0)
    assert result.returncode == 0 and len(calls) == 1


def test_go_verify_source_is_a_gate_path(repo: Path) -> None:
    gate = gate_wiring_check._shared_gate()
    writer = 'Writer: mimo-v2.6-flash run-1\n'
    seed = _commit(repo, 'chore: seed\n\n' + writer, {'README.md': 'x\n'})
    path = 'rust/plangraph-cli/src/land/go_verify.rs'
    sha = _commit(repo, 'feat: x\n\n' + writer, {path: '// x\n'})
    assert gate.check(seed, sha, cwd=repo) == [f'{sha}: gate edit without Gate-Edit trailer']
    # Its sibling test file is not a gate path: only the verifier source and its pin are.
    sha2 = _commit(repo, 'feat: y\n\n' + writer, {'rust/plangraph-cli/src/land/go_verify_tests.rs': '// y\n'})
    assert gate.check(sha, sha2, cwd=repo) == []
