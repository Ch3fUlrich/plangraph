'''Check CI gate wiring changes for Gate-Edit trailers.'''
from __future__ import annotations

import argparse
import difflib
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml


class GateError(Exception):
    '''An error that should terminate main with a specific code.'''

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class MissingJob(Exception):
    '''The requested job is absent from a workflow revision.'''

    def __init__(self, job: str, rev: str) -> None:
        super().__init__(f'{job} job missing at {rev}')
        self.job = job
        self.rev = rev


def _git_show(rev: str, workflow: str, cwd: str | Path | None) -> str:
    command = ['git', 'show', f'{rev}:{workflow}']
    try:
        result = subprocess.run(command, cwd=cwd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        message = exc.stderr.strip() or f'git show failed for {rev}:{workflow}'
        raise GateError(message, 2) from exc
    return result.stdout


def wiring_at(rev: str, workflow: str, job: str, cwd: str | Path | None = None) -> dict[str, object]:
    '''Return the wiring for job at rev, raising MissingJob when absent.'''
    text = _git_show(rev, workflow, cwd)
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise GateError(f'YAML parse error at {rev}: {exc}', 2) from exc
    if not isinstance(doc, dict):
        raise GateError(f'workflow at {rev} is not a mapping', 2)
    jobs = doc.get('jobs')
    if not isinstance(jobs, dict) or job not in jobs:
        raise MissingJob(job, rev)
    triggers = doc.get('on', doc.get(True))
    return {'on': triggers, 'job': jobs[job]}


def _git_out(args: list[str], cwd: str | Path | None) -> str:
    command = ['git', *args]
    try:
        result = subprocess.run(command, cwd=cwd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        message = exc.stderr.strip() or f'git command failed: {" ".join(command)}'
        raise GateError(message, 2) from exc
    return result.stdout


_TRAILER_LINE = re.compile(r'[A-Za-z][A-Za-z0-9-]*: \S')
_GATE_EDIT = re.compile(r'Gate-Edit: D-\d+')


def _trailer_block(message: str) -> list[str]:
    '''Return the trailing paragraphs whose every line is a trailer line.'''
    lines = message.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    paragraphs: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if not line.strip():
            if current:
                paragraphs.append(current)
                current = []
        else:
            current.append(line)
    if current:
        paragraphs.append(current)
    block: list[str] = []
    for paragraph in reversed(paragraphs):
        if not all(_TRAILER_LINE.match(line) for line in paragraph):
            break
        block = paragraph + block
    return block


def _commit_shas(base: str, head: str, cwd: str | Path | None, include_merges: bool = False) -> list[str]:
    text = _git_out(['log', *([] if include_merges else ['--no-merges']), '--format=%H', f'{base}..{head}'], cwd)
    return [line.strip() for line in text.splitlines() if line.strip()]


def _commit_message(sha: str, cwd: str | Path | None) -> str:
    return _git_out(['show', '-s', '--format=%B', sha], cwd)


def _shared_gate():
    # One implementation of "what a merge edits" lives in ci_commit_gate; load it beside this file.
    import importlib.util
    spec = importlib.util.spec_from_file_location('ci_commit_gate', Path(__file__).with_name('ci_commit_gate.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _commit_paths(sha: str, cwd: str | Path | None = None, include_merges: bool = False) -> list[str]:
    if include_merges:
        # Combined diff plus the paths a merge's parents conflict on (a side kept whole shows in neither).
        try:
            return _shared_gate().merge_edited_paths(sha, cwd=cwd)
        except Exception as exc:  # the shared reader's GateError is a different class per load
            raise GateError(str(exc), 2) from exc
    text = _git_out(['diff-tree', '--no-commit-id', '--name-only', '-r', '-z', '--root', sha], cwd)
    return [path for path in text.split('\x00') if path]


def has_gate_edit(base: str, head: str, cwd: str | Path | None = None) -> bool:
    '''Return True when any commit in base..head has a valid Gate-Edit trailer.'''
    for sha in _commit_shas(base, head, cwd):
        message = _commit_message(sha, cwd)
        if any(_GATE_EDIT.fullmatch(line) for line in _trailer_block(message)):
            return True
    return False


def commits_touching(base: str, head: str, workflow: str, cwd: str | Path | None = None,
                     include_merges: bool = False) -> list[tuple[str, bool]]:
    '''Return (sha, has_gate_edit) for commits in base..head touching workflow.'''
    touching: list[tuple[str, bool]] = []
    for sha in _commit_shas(base, head, cwd, include_merges):
        message = _commit_message(sha, cwd)
        paths = _commit_paths(sha, cwd, include_merges)
        if workflow not in paths:
            continue
        has_edit = any(_GATE_EDIT.fullmatch(line) for line in _trailer_block(message))
        touching.append((sha, has_edit))
    return touching


def commits_missing_gate_edit(base: str, head: str, workflow: str, cwd: str | Path | None = None,
                             include_merges: bool = False) -> list[str]:
    '''Return shas in base..head that touch workflow without a strict Gate-Edit line in their message.'''
    return [sha for sha, has_edit in commits_touching(
        base, head, workflow, cwd=cwd, include_merges=include_merges) if not has_edit]


GO_VERIFY_COMMAND = 'land-train go-verify --evidence'


def go_verify_step(job: object) -> dict | None:
    '''Return the step of a ci-gate job that runs the go-verify verifier, or None.'''
    steps = job.get('steps') if isinstance(job, dict) else None
    for step in steps if isinstance(steps, list) else []:
        if isinstance(step, dict) and GO_VERIFY_COMMAND in str(step.get('run', '')):
            return step
    return None


def go_verify_problems(base_job: object, head_job: object) -> list[str]:
    '''Return why the head weakens a go-verify step the base already has (none when base has none).'''
    base_step = go_verify_step(base_job)
    if base_step is None:
        return []
    head_step = go_verify_step(head_job)
    if head_step is None:
        return ['go-verify step removed']
    problems = []
    if head_step.get('continue-on-error'):
        problems.append('go-verify step made non-blocking (continue-on-error)')
    if re.search(r'\|\|\s*(true|:)\b|\bset \+e\b', str(head_step.get('run', ''))):
        problems.append('go-verify step failure is swallowed (|| true or set +e)')
    if head_step.get('if') != base_step.get('if'):
        problems.append('go-verify step condition changed')
    return problems


def _wiring_diff(base_wiring: dict[str, object], head_wiring: dict[str, object]) -> str:
    base_text = json.dumps(base_wiring, indent=2, sort_keys=True)
    head_text = json.dumps(head_wiring, indent=2, sort_keys=True)
    lines = difflib.unified_diff(
        base_text.splitlines(),
        head_text.splitlines(),
        fromfile='base',
        tofile='head',
        lineterm='',
    )
    return '\n'.join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Check CI gate wiring changes for Gate-Edit trailers.')
    parser.add_argument('--base', required=True)
    parser.add_argument('--head', required=True)
    parser.add_argument('--workflow', default='.github/workflows/tests.yml')
    parser.add_argument('--job', default='ci-gate')
    args = parser.parse_args(argv)
    try:
        base_wiring = wiring_at(args.base, args.workflow, args.job)
    except MissingJob:
        print(f'{args.job} job missing at base', file=sys.stderr)
        return 2
    except GateError as exc:
        print(exc.message, file=sys.stderr)
        return exc.code
    try:
        head_wiring = wiring_at(args.head, args.workflow, args.job)
    except MissingJob:
        print(f'{args.job} job missing at head', file=sys.stderr)
        return 1
    except GateError as exc:
        print(exc.message, file=sys.stderr)
        return exc.code
    if base_wiring == head_wiring:
        print('gate wiring unchanged')
        return 0
    print(_wiring_diff(base_wiring, head_wiring))
    problems = go_verify_problems(base_wiring['job'], head_wiring['job'])
    if problems:
        # A Gate-Edit trailer does not excuse weakening the verifier of a computed GO.
        print('; '.join(problems), file=sys.stderr)
        return 1
    try:
        missing = commits_missing_gate_edit(args.base, args.head, args.workflow)
        touching = commits_touching(args.base, args.head, args.workflow)
    except GateError as exc:
        print(exc.message, file=sys.stderr)
        return exc.code
    if not touching:
        print('gate wiring changed WITHOUT Gate-Edit', file=sys.stderr)
        return 1
    if not missing:
        print('gate wiring changed WITH Gate-Edit: review the diff')
        return 0
    print('gate wiring changed WITHOUT Gate-Edit: ' + ' '.join(missing), file=sys.stderr)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
